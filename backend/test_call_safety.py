import asyncio
import contextlib
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.converters.llvc_stream import LLVCStreamingConverter
from backend.pipeline import VoiceConversionWorker


def _load_main():
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for key in (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "RVC_ENDPOINT_URL",
        "RVC_API_KEY",
        "LLVC_PILOT_ENABLED",
        "LLVC_WS_URL",
        "LLVC_API_KEY",
        "NS_LEVEL",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_PHONE_NUMBER",
    ):
        os.environ.pop(key, None)
    from backend import main

    return main


class _LLVCHealthStub:
    def __init__(self, healthy: bool):
        self.is_healthy = healthy

    async def convert_stream(self, in_audio):
        if False:
            yield b""


def _make_llvc_worker(*, healthy: bool) -> VoiceConversionWorker:
    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=_LLVCHealthStub(healthy),
        suppressor=MagicMock(),
        requested_engine="llvc",
        effective_engine="llvc",
    )
    worker._WATCHDOG_POLL_S = 0.01
    worker._LLVC_OUTAGE_TIMEOUT_S = 0.05
    return worker


class LLVCOutageDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def _run_watchdog(self, worker, duration: float = 0.25) -> AsyncMock:
        callback = AsyncMock()
        worker.on_llvc_fatal_failure = callback
        task = asyncio.create_task(worker._holding_watchdog())
        try:
            await asyncio.sleep(duration)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return callback

    async def test_pre_call_idle_never_triggers_fatal_outage(self):
        worker = _make_llvc_worker(healthy=False)
        worker._last_chunk_at = time.monotonic() - 10.0

        callback = await self._run_watchdog(worker)

        callback.assert_not_awaited()

    async def test_muted_microphone_without_pending_input_never_triggers(self):
        worker = _make_llvc_worker(healthy=False)
        now = time.monotonic()
        worker._last_submitted_input_at = now
        worker._last_converted_output_at = now

        callback = await self._run_watchdog(worker)

        callback.assert_not_awaited()

    async def test_normal_speech_pause_while_converter_is_healthy_never_triggers(self):
        worker = _make_llvc_worker(healthy=True)
        worker._last_submitted_input_at = time.monotonic()
        worker._last_converted_output_at = 0.0

        callback = await self._run_watchdog(worker)

        callback.assert_not_awaited()

    async def test_unhealthy_converter_with_unacknowledged_input_triggers_after_timeout(self):
        worker = _make_llvc_worker(healthy=False)
        worker._last_submitted_input_at = time.monotonic()
        worker._last_converted_output_at = 0.0

        callback = await self._run_watchdog(worker)

        callback.assert_awaited_once()

    async def test_fatal_cleanup_callback_failure_is_observed(self):
        worker = _make_llvc_worker(healthy=False)
        worker._last_submitted_input_at = time.monotonic()
        worker._last_converted_output_at = 0.0
        worker.on_llvc_fatal_failure = AsyncMock(
            side_effect=RuntimeError("cleanup failed")
        )
        task = asyncio.create_task(worker._holding_watchdog())
        with patch("builtins.print") as print_mock:
            try:
                await asyncio.sleep(0.12)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await asyncio.sleep(0)

        self.assertTrue(
            any("fatal cleanup callback failed: RuntimeError" in str(call)
                for call in print_mock.call_args_list)
        )


class LLVCRealSessionReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_ready_uses_the_active_conversion_session(self):
        connection_count = 0

        class FakeWebSocket:
            async def send(self, _message):
                return None

            async def recv(self):
                return '{"type":"ready"}'

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

        class FakeConnection:
            async def __aenter__(self):
                nonlocal connection_count
                connection_count += 1
                return FakeWebSocket()

            async def __aexit__(self, *_args):
                return False

        converter = LLVCStreamingConverter(ws_url="ws://llvc.test/ws")

        async def idle_input():
            while True:
                await asyncio.sleep(10.0)
                if False:
                    yield b""

        with patch(
            "backend.converters.llvc_stream.websockets.connect",
            side_effect=lambda *_args, **_kwargs: FakeConnection(),
        ):
            stream = converter.convert_stream(idle_input())
            consume_task = asyncio.create_task(anext(stream))
            try:
                deadline = time.monotonic() + 1.0
                while connection_count == 0 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)

                self.assertTrue(await converter.wait_ready(0.5))
                self.assertEqual(connection_count, 1)
                self.assertTrue(converter.is_healthy)
            finally:
                await converter.close()
                with contextlib.suppress(StopAsyncIteration, asyncio.CancelledError):
                    await consume_task

        self.assertFalse(getattr(converter, "is_healthy", False))


class ControlPlaneSafetyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _load_main()

    def test_noise_suppression_defaults_to_level_one(self):
        self.assertEqual(self.main.NS_LEVEL, 1)

    async def test_missing_rvc_configuration_fails_closed_by_default(self):
        token_builder = MagicMock()
        token_builder.with_identity.return_value = token_builder
        token_builder.with_name.return_value = token_builder
        token_builder.with_grants.return_value = token_builder
        token_builder.with_ttl.return_value = token_builder
        token_builder.to_jwt.return_value = "test-token"

        fake_worker = MagicMock()
        fake_worker.running = False
        fake_worker.room.isconnected.return_value = False
        fake_worker.start = AsyncMock()
        fake_worker.stop = AsyncMock()

        with (
            patch.object(self.main, "LIVEKIT_API_KEY", "test-key"),
            patch.object(self.main, "LIVEKIT_API_SECRET", "test-secret"),
            patch.object(self.main, "RVC_ENDPOINT_URL", None),
            patch.object(self.main.api, "AccessToken", return_value=token_builder),
            patch.object(self.main, "VoiceConversionWorker", return_value=fake_worker),
        ):
            with self.assertRaises(self.main.HTTPException) as raised:
                await self.main._do_start_bot(
                    "outbound_missing_rvc",
                    requested_engine="rvc",
                )

        self.assertEqual(raised.exception.status_code, 503)

    async def test_explicit_development_dummy_is_never_reported_as_rvc(self):
        token_builder = MagicMock()
        token_builder.with_identity.return_value = token_builder
        token_builder.with_name.return_value = token_builder
        token_builder.with_grants.return_value = token_builder
        token_builder.with_ttl.return_value = token_builder
        token_builder.to_jwt.return_value = "test-token"

        fake_worker = MagicMock()
        fake_worker.running = False
        fake_worker.room.isconnected.return_value = False
        fake_worker.start = AsyncMock()
        fake_worker.stop = AsyncMock()
        worker_factory = MagicMock(return_value=fake_worker)

        with (
            patch.object(self.main, "LIVEKIT_API_KEY", "test-key"),
            patch.object(self.main, "LIVEKIT_API_SECRET", "test-secret"),
            patch.object(self.main, "RVC_ENDPOINT_URL", None),
            patch.object(self.main, "ALLOW_DUMMY_CONVERTER", True, create=True),
            patch.object(self.main.api, "AccessToken", return_value=token_builder),
            patch.object(self.main, "VoiceConversionWorker", worker_factory),
        ):
            await self.main._do_start_bot(
                "development_dummy",
                requested_engine="rvc",
            )
            await asyncio.sleep(0)

        self.assertEqual(
            worker_factory.call_args.kwargs["effective_engine"],
            "dummy",
        )

    async def test_pstn_rejects_dummy_even_when_development_dummy_is_enabled(self):
        with (
            patch.object(self.main, "LIVEKIT_API_KEY", "test-key"),
            patch.object(self.main, "LIVEKIT_API_SECRET", "test-secret"),
            patch.object(self.main, "RVC_ENDPOINT_URL", None),
            patch.object(self.main, "ALLOW_DUMMY_CONVERTER", True),
        ):
            try:
                await self.main._do_start_bot(
                    "outbound_no_real_engine",
                    requested_engine="rvc",
                    require_real_engine=True,
                )
            except TypeError:
                self.fail("PSTN startup must explicitly require a real converter")
            except self.main.HTTPException as raised:
                self.assertEqual(raised.status_code, 503)
            else:
                self.fail("PSTN startup must not use the development dummy")

    async def test_outbound_falls_back_from_unready_real_llvc_session_to_rvc(self):
        llvc_worker = MagicMock()
        llvc_worker.requested_engine = "llvc"
        llvc_worker.effective_engine = "llvc"
        llvc_worker.fallback_reason = None
        llvc_worker.model_version = "llvc-pilot"
        llvc_worker.wait_for_readiness_probe = AsyncMock(return_value=False)

        rvc_worker = MagicMock()
        rvc_worker.requested_engine = "llvc"
        rvc_worker.effective_engine = "rvc"
        rvc_worker.fallback_reason = "LLVC call session was not ready"
        rvc_worker.model_version = "rvc-test"
        rvc_worker.wait_for_readiness_probe = AsyncMock(return_value=True)

        async def start_bot(room_name, *_args, **_kwargs):
            worker = llvc_worker if start_mock.await_count == 1 else rvc_worker
            self.main.active_workers[room_name] = worker
            return {"status": "started"}

        async def cleanup(room_name, **_kwargs):
            self.main.active_workers.pop(room_name, None)

        token_builder = MagicMock()
        token_builder.with_identity.return_value = token_builder
        token_builder.with_name.return_value = token_builder
        token_builder.with_grants.return_value = token_builder
        token_builder.with_ttl.return_value = token_builder
        token_builder.to_jwt.return_value = "agent-token"
        start_mock = AsyncMock(side_effect=start_bot)

        request = self.main.OutboundCallRequest(
            phoneNumber="+15551234567",
            agentIdentity="agent-test",
            voiceEngine="llvc",
        )
        self.main.active_workers.clear()
        self.main.active_calls.clear()
        with (
            patch.object(self.main, "LIVEKIT_API_KEY", "test-key"),
            patch.object(self.main, "LIVEKIT_API_SECRET", "test-secret"),
            patch.object(self.main, "_do_start_bot", start_mock),
            patch.object(self.main, "_wait_for_worker_started", AsyncMock()),
            patch.object(self.main, "_cleanup_room_state", AsyncMock(side_effect=cleanup)),
            patch.object(self.main.api, "AccessToken", return_value=token_builder),
        ):
            try:
                response = await self.main.call_outbound(request)
            except self.main.HTTPException as exc:
                self.fail(f"unready LLVC should fall back before dialing, got {exc.status_code}")

        self.assertEqual(start_mock.await_count, 2)
        self.assertEqual(response["requested_engine"], "llvc")
        self.assertEqual(response["effective_engine"], "rvc")
        self.assertEqual(response["fallback_reason"], "LLVC call session was not ready")
        self.assertEqual(response["model_version"], "rvc-test")
        persisted = self.main.active_calls[response["roomName"]]
        self.assertEqual(persisted["requested_engine"], "llvc")
        self.assertEqual(persisted["effective_engine"], "rvc")
        self.assertEqual(persisted["model_version"], "rvc-test")

    def test_outbound_room_ids_are_collision_resistant(self):
        room_ids = {
            self.main._new_outbound_room_name("+15551234567")
            for _ in range(100)
        }

        self.assertEqual(len(room_ids), 100)
        self.assertTrue(all(room.startswith("outbound_15551234567_") for room in room_ids))

    async def test_twilio_hangup_runs_off_the_event_loop(self):
        room_name = "inbound_CA123"
        call_handle = MagicMock()
        twilio = MagicMock()
        twilio.calls.return_value = call_handle
        self.main.active_calls[room_name] = {
            "direction": "inbound",
            "call_sid": "CA123",
        }

        with (
            patch.object(self.main, "twilio_client", twilio),
            patch.object(self.main, "LIVEKIT_API_KEY", None),
            patch.object(self.main, "LIVEKIT_API_SECRET", None),
            patch.object(self.main, "_cleanup_room_state", AsyncMock()),
            patch.object(self.main.manager, "broadcast", AsyncMock()),
            patch.object(self.main.asyncio, "to_thread", AsyncMock()) as to_thread,
        ):
            await self.main._do_end_call(room_name)

        to_thread.assert_awaited_once_with(call_handle.update, status="completed")

    async def test_background_cleanup_task_failures_are_observed(self):
        async def fail_cleanup():
            raise RuntimeError("cleanup exploded")

        coroutine = fail_cleanup()
        with patch("builtins.print") as print_mock:
            try:
                task = self.main._spawn_observed_task(
                    coroutine,
                    name="test-cleanup",
                )
            except AttributeError:
                coroutine.close()
                self.fail("background cleanup tasks need an observed-task helper")
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        self.assertTrue(task.done())
        self.assertTrue(
            any("test-cleanup failed: RuntimeError" in str(call) for call in print_mock.call_args_list)
        )

    async def test_inbound_accept_returns_and_persists_engine_state(self):
        room_name = "inbound_CA456"
        worker = MagicMock()
        worker.requested_engine = "llvc"
        worker.effective_engine = "rvc"
        worker.fallback_reason = "LLVC call session was not ready"
        worker.model_version = "rvc-test"
        self.main.active_calls[room_name] = {"status": "ringing"}

        token_builder = MagicMock()
        token_builder.with_identity.return_value = token_builder
        token_builder.with_name.return_value = token_builder
        token_builder.with_grants.return_value = token_builder
        token_builder.with_ttl.return_value = token_builder
        token_builder.to_jwt.return_value = "agent-token"
        request = self.main.AcceptCallRequest(
            roomName=room_name,
            agentIdentity="agent-test",
            voiceEngine="llvc",
        )

        with (
            patch.object(self.main, "LIVEKIT_API_KEY", "test-key"),
            patch.object(self.main, "LIVEKIT_API_SECRET", "test-secret"),
            patch.object(self.main, "_do_start_bot", AsyncMock()),
            patch.object(self.main, "_wait_for_worker_started", AsyncMock()),
            patch.object(
                self.main,
                "_ensure_pstn_worker_ready",
                AsyncMock(return_value=worker),
            ),
            patch.object(self.main, "_restrict_sip_audio", AsyncMock(return_value=True)),
            patch.object(self.main.api, "AccessToken", return_value=token_builder),
        ):
            response = await self.main.call_accept(request)
            await asyncio.sleep(0)

        self.assertEqual(response["requested_engine"], "llvc")
        self.assertEqual(response["effective_engine"], "rvc")
        self.assertEqual(response["fallback_reason"], "LLVC call session was not ready")
        self.assertEqual(response["model_version"], "rvc-test")
        self.assertEqual(self.main.active_calls[room_name]["model_version"], "rvc-test")


if __name__ == "__main__":
    unittest.main()
