import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

def _load_main():
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for key in (
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "RVC_ENDPOINT_URL",
        "RVC_API_KEY",
        "NS_LEVEL",
        "TWILIO_ACCOUNT_SID",
        "TWILIO_AUTH_TOKEN",
        "TWILIO_PHONE_NUMBER",
    ):
        os.environ.pop(key, None)
    from backend import main

    return main


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
        worker.requested_engine = "rvc"
        worker.effective_engine = "dummy"
        worker.fallback_reason = "RVC endpoint unready"
        worker.model_version = "dummy-development"
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
            voiceEngine="rvc",
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

        self.assertEqual(response["requested_engine"], "rvc")
        self.assertEqual(response["effective_engine"], "dummy")
        self.assertEqual(response["fallback_reason"], "RVC endpoint unready")
        self.assertEqual(response["model_version"], "dummy-development")
        self.assertEqual(self.main.active_calls[room_name]["model_version"], "dummy-development")


if __name__ == "__main__":
    unittest.main()
