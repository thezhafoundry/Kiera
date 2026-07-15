import asyncio
import json
import time
import unittest

from backend.converters.llvc_stream import LLVCStreamingConverter
from backend.converters.rvc_stream import RVCStreamingConverter
from backend.pipeline import VoiceConversionWorker


class _RecordingSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class StreamingConverterSafetyTests(unittest.IsolatedAsyncioTestCase):
    def _converters(self, **kwargs):
        return (
            RVCStreamingConverter(ws_url="ws://127.0.0.1:1/ws", **kwargs),
            LLVCStreamingConverter(ws_url="ws://127.0.0.1:1/ws", **kwargs),
        )

    def test_remote_plaintext_websocket_is_rejected(self):
        for converter_type in (RVCStreamingConverter, LLVCStreamingConverter):
            with self.subTest(converter=converter_type.__name__):
                with self.assertRaisesRegex(ValueError, "wss"):
                    converter_type(
                        ws_url="ws://voice.example.com/ws",
                        api_key="secret",
                    )

    def test_remote_websocket_requires_api_key_but_loopback_does_not(self):
        for converter_type in (RVCStreamingConverter, LLVCStreamingConverter):
            with self.subTest(converter=converter_type.__name__):
                with self.assertRaisesRegex(ValueError, "API key"):
                    converter_type(ws_url="wss://voice.example.com/ws")
                converter_type(ws_url="ws://localhost:8765/ws")
                converter_type(ws_url="ws://[::1]:8765/ws")

    def test_authentication_uses_only_authorization_header(self):
        for converter_type in (RVCStreamingConverter, LLVCStreamingConverter):
            with self.subTest(converter=converter_type.__name__):
                converter = converter_type(
                    ws_url="wss://voice.example.com/ws",
                    api_key="secret",
                )
                config = json.loads(converter._config_payload())
                self.assertNotIn("api_key", config)
                self.assertEqual(
                    converter._connect_kwargs()["additional_headers"],
                    {"Authorization": "Bearer secret"},
                )

    def test_websocket_transport_has_bounded_messages_and_heartbeat(self):
        for converter in self._converters(
            max_message_size=12345,
            heartbeat_interval=0.25,
            heartbeat_timeout=0.5,
        ):
            with self.subTest(converter=type(converter).__name__):
                kwargs = converter._connect_kwargs()
                self.assertEqual(kwargs["max_size"], 12345)
                self.assertEqual(kwargs["ping_interval"], 0.25)
                self.assertEqual(kwargs["ping_timeout"], 0.5)

    def test_dead_peer_marks_session_unhealthy_and_counts_failure(self):
        for converter in self._converters():
            with self.subTest(converter=type(converter).__name__):
                converter._is_healthy = True
                converter._mark_connection_lost("heartbeat timeout")

                self.assertFalse(converter.is_healthy)
                self.assertEqual(converter.connection_failure_count, 1)

    async def test_stale_input_is_dropped_immediately_before_send(self):
        for converter in self._converters():
            with self.subTest(converter=type(converter).__name__):
                converter._buffer_lock = asyncio.Lock()
                converter._buffer_not_empty = asyncio.Event()
                converter._input_exhausted = True
                converter._closed = False
                await converter._buffer_input(
                    b"stale",
                    enqueued_at=time.monotonic() - 0.6,
                )
                await converter._buffer_input(b"fresh")
                socket = _RecordingSocket()

                await converter._writer_loop(socket)

                self.assertEqual(socket.sent, [b"fresh"])
                self.assertEqual(converter.stale_input_drop_count, 1)

    async def test_converted_output_queue_drops_oldest_and_counts(self):
        for converter in self._converters(output_queue_max_chunks=2):
            with self.subTest(converter=type(converter).__name__):
                converter._out_queue = asyncio.Queue(maxsize=2)

                await converter._handle_incoming(None, b"converted-1")
                await converter._handle_incoming(None, b"converted-2")
                await converter._handle_incoming(None, b"converted-3")

                self.assertEqual(converter._out_queue.get_nowait(), b"converted-2")
                self.assertEqual(converter._out_queue.get_nowait(), b"converted-3")
                self.assertEqual(converter.output_drop_count, 1)

    async def test_stats_have_monotonic_sequence_and_model_version(self):
        for converter in self._converters(model_version="model-sha256"):
            with self.subTest(converter=type(converter).__name__):
                seen = []
                converter.on_stats = seen.append

                await converter._handle_incoming(
                    None, json.dumps({"type": "stats", "infer_ms": 1.0})
                )
                await converter._handle_incoming(
                    None, json.dumps({"type": "stats", "infer_ms": 2.0})
                )

                self.assertEqual(
                    [item["sequence_id"] for item in seen],
                    [1, 2],
                )
                self.assertEqual(
                    [item["model_version"] for item in seen],
                    ["model-sha256", "model-sha256"],
                )

    async def test_fatal_protocol_error_propagates_and_never_yields_raw(self):
        for converter in self._converters(output_queue_max_chunks=1):
            with self.subTest(converter=type(converter).__name__):
                converter._out_queue = asyncio.Queue(maxsize=1)
                converter._buffer_lock = asyncio.Lock()
                converter._buffer_not_empty = asyncio.Event()
                await converter._buffer_input(b"raw-representative-agent-audio")
                converter._is_healthy = True
                await converter._handle_incoming(None, json.dumps({
                    "type": "error",
                    "message": "model failed",
                    "fatal": True,
                }))
                await converter._handle_incoming(
                    None, b"late-converted-audio-after-fatal"
                )

                with self.assertRaisesRegex(RuntimeError, "model failed"):
                    await converter._next_output()
                self.assertFalse(converter.is_healthy)
                self.assertEqual(converter._out_queue.qsize(), 0)
                self.assertEqual(converter._buffered_bytes, 30)

    async def test_llvc_fatal_reaches_call_cleanup_before_stream_teardown(self):
        class FatalConverter:
            is_healthy = False
            on_stats = None

            async def convert_stream(self, in_audio):
                raise RuntimeError("LLVC streaming converter fatal error: dead peer")
                yield b""  # pragma: no cover - makes this an async generator

        worker = VoiceConversionWorker(
            room_url="ws://unused",
            token="unused",
            converter=FatalConverter(),
            suppressor=None,
            requested_engine="llvc",
            effective_engine="llvc",
        )
        cleaned_up = asyncio.Event()

        async def on_fatal():
            cleaned_up.set()

        worker.on_llvc_fatal_failure = on_fatal

        await worker._run_conversion_stream()

        await asyncio.wait_for(cleaned_up.wait(), timeout=0.2)

    async def test_close_awaits_all_background_task_teardown(self):
        for converter in self._converters(output_queue_max_chunks=1):
            with self.subTest(converter=type(converter).__name__):
                converter._out_queue = asyncio.Queue(maxsize=1)
                converter._pump_task = asyncio.create_task(asyncio.sleep(60))
                converter._conn_task = asyncio.create_task(asyncio.sleep(60))

                await converter.close()

                self.assertTrue(converter._pump_task.done())
                self.assertTrue(converter._conn_task.done())
                self.assertFalse(converter.is_healthy)


if __name__ == "__main__":
    unittest.main()
