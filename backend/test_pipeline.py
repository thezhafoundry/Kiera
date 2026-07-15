import asyncio
import contextlib
import json
import logging
import numpy as np
import time
from unittest.mock import AsyncMock, patch, MagicMock
import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from backend.noise.noise_suppressor import WebRTCNoiseSuppressor
from backend.converters.dummy import DummyVoiceConverter
from backend.converters.rvc import RVCVoiceConverter
from backend.converters.rvc_stream import RVCStreamingConverter, _MAX_BUFFER_BYTES
from backend.pipeline import VoiceConversionWorker

async def test_noise_suppressor():
    print("\n--- Testing Noise Suppressor ---")
    
    # Initialize WebRTC Noise Suppressor
    suppressor = WebRTCNoiseSuppressor(ns_level=3)
    
    # 10ms of 16kHz mono audio is 160 samples (320 bytes)
    # Generate a dummy frame with silent and loud random noise
    np.random.seed(42)
    noise_frame = np.random.randint(-10000, 10000, 160, dtype=np.int16).tobytes()
    
    # Process frame
    start_t = time.perf_counter()
    denoised_frame = suppressor.process_frame(noise_frame)
    duration_ms = (time.perf_counter() - start_t) * 1000.0
    
    print(f"WebRTC Denoising took {duration_ms:.3f}ms")
    print(f"Input size: {len(noise_frame)} bytes, Output size: {len(denoised_frame)} bytes")
    assert len(denoised_frame) == len(noise_frame), "Output size mismatch!"
    
    # Check if amplitude was suppressed
    in_amp = np.mean(np.abs(np.frombuffer(noise_frame, dtype=np.int16)))
    out_amp = np.mean(np.abs(np.frombuffer(denoised_frame, dtype=np.int16)))
    print(f"Input average amplitude: {in_amp:.1f}")
    print(f"Denoised average amplitude: {out_amp:.1f}")
    # When the native webrtc-noise-gain lib is present the suppressor must reduce
    # amplitude; when it's missing (e.g. Windows dev boxes) the suppressor degrades
    # to passthrough, so only assert suppression if the processor is actually active.
    if getattr(suppressor, "_active", False):
        assert out_amp < in_amp, "Noise was not suppressed!"
        print("Noise Suppressor Test: SUCCESS (native suppression active)")
    else:
        assert out_amp == in_amp, "Passthrough must not alter audio when suppressor is inactive!"
        print("Noise Suppressor Test: SUCCESS (passthrough — native lib unavailable)")

async def test_dummy_converter():
    print("\n--- Testing Dummy Voice Converter ---")
    
    converter = DummyVoiceConverter(carrier_frequency=120.0)
    
    # Generate 500ms of input audio (50 frames of 10ms = 8000 samples @ 16kHz)
    t = np.linspace(0, 0.5, 8000, endpoint=False)
    sine_wave = (np.sin(2 * np.pi * 440 * t) * 10000).astype(np.int16) # 440Hz tone
    input_bytes = sine_wave.tobytes()
    
    # Create an async generator that yields the input audio in 10ms (320 bytes) chunks
    async def chunk_generator():
        chunk_size = 320
        for i in range(0, len(input_bytes), chunk_size):
            yield input_bytes[i:i+chunk_size]
            await asyncio.sleep(0.001) # simulate brief streaming arrival

    start_t = time.perf_counter()
    converted_chunks = []
    
    async for converted_chunk in converter.convert_stream(chunk_generator()):
        converted_chunks.append(converted_chunk)
        
    duration_ms = (time.perf_counter() - start_t) * 1000.0
    output_bytes = b"".join(converted_chunks)
    
    print(f"Dummy conversion took {duration_ms:.2f}ms for 500ms of audio")
    print(f"Input size: {len(input_bytes)} bytes, Output size: {len(output_bytes)} bytes")
    # DummyVoiceConverter now upsamples 16kHz -> 48kHz (3x) to match the
    # streaming converter output contract (see backend/converters/dummy.py).
    assert len(output_bytes) == len(input_bytes) * 3, "Conversion output size mismatch!"

    # Ensure audio was modified. Undo the 3x sample repeat before comparing so
    # the two arrays are the same shape as the original 16kHz signal.
    in_arr = np.frombuffer(input_bytes, dtype=np.int16)
    out_arr = np.frombuffer(output_bytes, dtype=np.int16)[::3]
    diff = np.mean(np.abs(in_arr - out_arr))
    print(f"Average signal modification delta: {diff:.1f}")
    assert diff > 10.0, "Audio was not modified by the dummy converter!"
    print("Dummy Voice Converter Test: SUCCESS")

async def test_rvc_converter_mocked():
    print("\n--- Testing RVC Converter with Mocked GPU Endpoint ---")
    
    endpoint_url = "https://mock-rvc-gpu-endpoint.modal.run"
    converter = RVCVoiceConverter(
        endpoint_url=endpoint_url,
        api_key="mock_secret_key",
        pitch_shift=2,
        budget_ms=3000.0
    )
    
    # Generate 400ms of dummy input audio
    t = np.linspace(0, 0.4, 6400, endpoint=False)
    input_bytes = (np.sin(2 * np.pi * 440 * t) * 5000).astype(np.int16).tobytes()
    
    async def chunk_generator():
        yield input_bytes

    # Create dummy mock response PCM bytes (half-amplitude as modification)
    mocked_pcm = (np.frombuffer(input_bytes, dtype=np.int16) // 2).tobytes()

    # Mock the HTTP POST request to the remote convert endpoint
    mock_request = httpx.Request("POST", f"{endpoint_url}/convert")
    mock_response = httpx.Response(200, content=mocked_pcm, request=mock_request)
    
    with patch.object(converter.client, 'post', new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response
        
        start_t = time.perf_counter()
        converted_chunks = []
        async for chunk in converter.convert_stream(chunk_generator()):
            converted_chunks.append(chunk)
            
        duration_ms = (time.perf_counter() - start_t) * 1000.0
        output_bytes = b"".join(converted_chunks)
        
        # Verify the mock endpoint was hit with headers and parameters.
        # RVCVoiceConverter POSTs to endpoint_url verbatim (the deployed URL already
        # includes the /convert path), so assert against endpoint_url directly.
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == endpoint_url
        assert kwargs["params"] == {"pitch_shift": 2}
        assert "Authorization" in kwargs["headers"]
        assert kwargs["headers"]["Authorization"] == "Bearer mock_secret_key"
        
        print(f"RVC conversion (mocked) completed in {duration_ms:.2f}ms")
        print(f"Input size: {len(input_bytes)} bytes, Output size: {len(output_bytes)} bytes")
        
        assert len(output_bytes) == len(mocked_pcm), "Mock output size mismatch!"
        assert output_bytes == mocked_pcm, "Output bytes do not match expected mocked response!"
        
    await converter.close()
    print("RVC Voice Converter Test: SUCCESS")

async def test_rvc_converter_empty_response():
    print("\n--- Testing RVC Converter Empty-Response Leak Fix ---")

    endpoint_url = "https://mock-rvc-gpu-endpoint.modal.run"
    converter = RVCVoiceConverter(
        endpoint_url=endpoint_url,
        api_key="mock_secret_key",
        pitch_shift=2,
        budget_ms=3000.0
    )

    # Enough input to clear the >=640-byte buffered-input gate, so the empty
    # response is what's actually under test (not the too-little-input gate).
    t = np.linspace(0, 0.4, 6400, endpoint=False)
    input_bytes = (np.sin(2 * np.pi * 440 * t) * 5000).astype(np.int16).tobytes()

    async def chunk_generator():
        yield input_bytes

    # A 200 OK with an empty body -- the endpoint ran but produced nothing.
    # Before the fix this used to fall through to leaking the raw input PCM.
    mock_request = httpx.Request("POST", f"{endpoint_url}/convert")
    mock_response = httpx.Response(200, content=b"", request=mock_request)

    with patch.object(converter.client, 'post', new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_response

        converted_chunks = []
        async for chunk in converter.convert_stream(chunk_generator()):
            converted_chunks.append(chunk)

        mock_post.assert_called_once()

        print(f"Chunks yielded for empty HTTP response: {len(converted_chunks)}")
        assert len(converted_chunks) == 0, "Empty HTTP response must yield ZERO chunks (never leak raw PCM)!"

    await converter.close()
    print("RVC Voice Converter Empty-Response Test: SUCCESS")


# ---------------------------------------------------------------------------
# RVCStreamingConverter tests: exercised against a real, in-process fake
# `/ws` server (websockets.serve) rather than mocks, so the WS handshake,
# framing, ordering, and reconnect-with-backoff logic are all genuinely
# driven end-to-end.
# ---------------------------------------------------------------------------

async def _fake_rvc_ws_handler(websocket):
    """Minimal fake Modal /ws server: replies `{"type":"ready"}` to the config
    handshake, then echoes each received binary frame back x3-upsampled
    (mirrors the real 16kHz-in / 48kHz-out relationship -- no real RVC math
    needed for these tests)."""
    try:
        await websocket.recv()  # the JSON config handshake message
        await websocket.send(json.dumps({"type": "ready"}))
        async for message in websocket:
            if isinstance(message, (bytes, bytearray)):
                samples = np.frombuffer(message, dtype=np.int16)
                upsampled = np.repeat(samples, 3).astype(np.int16)
                await websocket.send(upsampled.tobytes())
            # ignore any stray text messages -- not sent by the client in these tests
    except ConnectionClosed:
        pass


def _make_frame(value: int, n_samples: int = 160) -> bytes:
    """A 320-byte (10ms @ 16kHz) frame whose samples all equal `value`, so the
    x3-upsampled echo can be identified and order-checked trivially."""
    return np.full(n_samples, value, dtype=np.int16).tobytes()


def _decode_frame_value(chunk: bytes) -> int:
    return int(np.frombuffer(chunk, dtype=np.int16)[0])


def _make_fed_input():
    """An async generator fed externally via an asyncio.Queue, so a test can
    control precisely when frames enter the converter's input pump (e.g. to
    send frames while a fake server is deliberately down)."""
    queue: asyncio.Queue = asyncio.Queue()

    async def gen():
        while True:
            item = await queue.get()
            yield item

    return gen(), queue


class _ListLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def _capture_logs(logger_name: str):
    """Captures log records from `logger_name` without silencing/duplicating
    the module's normal logging (restores level/handlers on exit)."""
    handler = _ListLogHandler()
    logger = logging.getLogger(logger_name)
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)


async def test_rvc_streaming_converter_basic():
    print("\n--- Testing RVCStreamingConverter: handshake, ordered echo, close() cleanliness ---")

    async with websockets.serve(_fake_rvc_ws_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        converter = RVCStreamingConverter(ws_url=f"ws://127.0.0.1:{port}/ws")

        in_gen, in_queue = _make_fed_input()
        output_chunks = []

        async def collect():
            async for chunk in converter.convert_stream(in_gen):
                output_chunks.append(chunk)

        with _capture_logs("backend.converters.rvc_stream") as log_handler:
            collect_task = asyncio.create_task(collect())

            values = [1, 2, 3, 4, 5]
            for v in values:
                in_queue.put_nowait(_make_frame(v))

            # Wait for the handshake + all 5 echoed frames.
            deadline = time.monotonic() + 5.0
            while len(output_chunks) < len(values) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

            assert len(output_chunks) == len(values), (
                f"expected {len(values)} echoed chunks, got {len(output_chunks)}"
            )
            for chunk, expected_value in zip(output_chunks, values):
                assert len(chunk) == 320 * 3, "echoed chunk should be x3-upsampled (16kHz PCM -> 48kHz PCM)"
                assert _decode_frame_value(chunk) == expected_value, "echoed chunks must arrive in send order"

            print(f"Handshake + ordered echo of {len(values)} frames: OK")

            await converter.close()
            await asyncio.wait_for(collect_task, timeout=5.0)
            # _make_fed_input's generator only unwinds via the pump task's
            # cancellation above; explicitly close it too so nothing relies
            # solely on that cancellation to release it.
            await in_gen.aclose()

            error_records = [r for r in log_handler.records if r.levelno >= logging.ERROR]
            assert not error_records, (
                f"unexpected ERROR-level logs after close(): {[r.getMessage() for r in error_records]}"
            )

        assert converter._pump_task.done() and converter._conn_task.done(), (
            "close() must leave no dangling background tasks"
        )

    print("RVCStreamingConverter basic (handshake/echo/close) test: SUCCESS")


async def test_rvc_streaming_converter_reconnect():
    print("\n--- Testing RVCStreamingConverter reconnect-with-backoff (never-raw during outage) ---")

    server = await websockets.serve(_fake_rvc_ws_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    converter = RVCStreamingConverter(ws_url=f"ws://127.0.0.1:{port}/ws")
    in_gen, in_queue = _make_fed_input()
    output_chunks = []

    async def collect():
        async for chunk in converter.convert_stream(in_gen):
            output_chunks.append(chunk)

    collect_task = asyncio.create_task(collect())
    server2 = None
    server1_closed = False
    try:
        # 1. Prove the happy path works before inducing an outage.
        in_queue.put_nowait(_make_frame(1))
        deadline = time.monotonic() + 3.0
        while len(output_chunks) < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert len(output_chunks) == 1, "pre-outage frame was not echoed"
        pre_outage_count = len(output_chunks)

        # 2. Kill the server mid-stream (graceful close, no OS-level process
        # kill needed -- Windows-friendly).
        server.close()
        await server.wait_closed()
        server1_closed = True

        # Frames sent during the outage are buffered only while they remain
        # within the 500ms live edge. These frames are deliberately left stale
        # to prove they are discarded before reconnect send, never replayed or
        # leaked raw.
        in_queue.put_nowait(_make_frame(2))
        in_queue.put_nowait(_make_frame(3))

        # 3. Outage window: poll continuously and assert NOTHING is yielded
        # while the server is down -- the never-raw invariant means silence,
        # not raw/leaked audio, during an outage.
        outage_window_s = 0.9
        outage_deadline = time.monotonic() + outage_window_s
        while time.monotonic() < outage_deadline:
            assert len(output_chunks) == pre_outage_count, (
                "converter yielded output during a WS outage -- never-raw invariant violated"
            )
            await asyncio.sleep(0.05)

        # 4. Restart a fake server on the SAME port and wait for the live
        # session handshake before submitting one fresh frame.
        server2 = await websockets.serve(_fake_rvc_ws_handler, "127.0.0.1", port)

        reconnect_deadline = time.monotonic() + 4.0
        while not converter.is_healthy and time.monotonic() < reconnect_deadline:
            await asyncio.sleep(0.02)
        assert converter.is_healthy, "converter did not establish its reconnect session"
        in_queue.put_nowait(_make_frame(4))

        expected_count = pre_outage_count + 1
        while len(output_chunks) < expected_count and time.monotonic() < reconnect_deadline:
            await asyncio.sleep(0.05)

        assert len(output_chunks) == expected_count, (
            f"expected only fresh post-reconnect audio: got {len(output_chunks)}, wanted {expected_count}"
        )
        assert _decode_frame_value(output_chunks[pre_outage_count]) == 4
        assert converter.stale_input_drop_count == 2
        print("Reconnect-with-backoff: stale outage audio dropped, fresh audio converted: OK")
    finally:
        if not server1_closed:
            server.close()
            await server.wait_closed()
        if server2 is not None:
            server2.close()
            await server2.wait_closed()
        await converter.close()
        await asyncio.wait_for(collect_task, timeout=5.0)
        await in_gen.aclose()

    print("RVCStreamingConverter reconnect test: SUCCESS")


async def test_rvc_streaming_converter_buffer_cap_drop_oldest():
    print("\n--- Testing RVCStreamingConverter reconnect buffer cap (bounded, drop-oldest) ---")

    active_websockets = []
    async def custom_handler(websocket):
        active_websockets.append(websocket)
        try:
            await websocket.recv()  # config handshake
            await websocket.send(json.dumps({"type": "ready"}))
            async for message in websocket:
                if isinstance(message, (bytes, bytearray)):
                    samples = np.frombuffer(message, dtype=np.int16)
                    upsampled = np.repeat(samples, 3).astype(np.int16)
                    await websocket.send(upsampled.tobytes())
        except ConnectionClosed:
            pass
        finally:
            if websocket in active_websockets:
                active_websockets.remove(websocket)

    server = await websockets.serve(custom_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    converter = RVCStreamingConverter(ws_url=f"ws://127.0.0.1:{port}/ws")
    in_gen, in_queue = _make_fed_input()
    output_chunks = []

    async def collect():
        async for chunk in converter.convert_stream(in_gen):
            output_chunks.append(chunk)

    collect_task = asyncio.create_task(collect())
    server2 = None
    server1_closed = False
    try:
        # 1. Prove the happy path works before inducing an outage.
        in_queue.put_nowait(_make_frame(0))
        deadline = time.monotonic() + 3.0
        while len(output_chunks) < 1 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert len(output_chunks) == 1, "pre-outage frame was not echoed"
        pre_outage_count = len(output_chunks)

        # 2. Kill the server so the connection loop cannot drain the buffer --
        # everything fed from here on stays in the bounded buffer until we
        # reconnect below.
        server.close()
        await server.wait_closed()
        server1_closed = True
        
        # Explicitly close any active connections to trigger client-side ConnectionClosed immediately
        for ws in list(active_websockets):
            await ws.close()
            
        await asyncio.sleep(0.6)  # Ensure connection is fully closed on client side before feeding

        # 3. Feed well over 500ms of 16kHz PCM (each frame is 320 bytes =
        # 10ms; _MAX_BUFFER_BYTES caps at 50 such frames = 500ms). Each frame's
        # sample value encodes its own sequence index, so which frames survive
        # the drop-oldest policy is externally verifiable after reconnect.
        frame_len_bytes = 320
        cap_frames = _MAX_BUFFER_BYTES // frame_len_bytes
        assert _MAX_BUFFER_BYTES % frame_len_bytes == 0, (
            "test assumes the cap is an exact multiple of the frame size"
        )
        n_fed = cap_frames + 200  # 2.5s fed against a 500ms cap
        for i in range(n_fed):
            in_queue.put_nowait(_make_frame(i))

        # 4. Wait for the input pump to drain the feeder queue and finish
        # applying the drop-oldest trim, then inspect internal buffer state
        # directly -- this is a white-box check of the cap/drop-oldest policy
        # itself, which the public API (order of replayed output) alone can't
        # fully prove (a drop-newest bug would still "pass" an output-only
        # check restricted to values within the surviving window).
        drain_deadline = time.monotonic() + 5.0
        while in_queue.qsize() > 0 and time.monotonic() < drain_deadline:
            await asyncio.sleep(0.01)
        assert in_queue.qsize() == 0, "feeder queue never drained -- pump task stalled"
        # Give the pump task a moment to finish appending/trimming the last
        # dequeued item (queue-empty doesn't itself guarantee that).
        await asyncio.sleep(0.1)

        assert converter._buffered_bytes == _MAX_BUFFER_BYTES, (
            f"expected buffer to sit exactly at the {_MAX_BUFFER_BYTES}-byte cap after "
            f"overflow, got {converter._buffered_bytes}"
        )
        assert len(converter._buffer) == cap_frames, (
            f"expected exactly {cap_frames} surviving frames, got {len(converter._buffer)}"
        )

        survivor_values = [_decode_frame_value(f.payload) for f in converter._buffer]
        expected_survivors = list(range(n_fed - cap_frames, n_fed))
        assert survivor_values == expected_survivors, (
            "drop-oldest policy violated: surviving buffered frames are not exactly the "
            f"newest {cap_frames} fed, in order. Expected {expected_survivors[:3]}...{expected_survivors[-3:]}, "
            f"got {survivor_values[:3]}...{survivor_values[-3:]}"
        )
        print(
            f"Buffer capped at {_MAX_BUFFER_BYTES} bytes ({cap_frames} frames); "
            f"oldest {n_fed - cap_frames} of {n_fed} fed frames dropped, newest survive in order: OK"
        )

        # 5. Reconnect. By now the bounded survivors are all older than 500ms,
        # so they must be discarded at the send boundary. Only audio submitted
        # after the new session handshake may be converted.
        server2 = await websockets.serve(_fake_rvc_ws_handler, "127.0.0.1", port)

        reconnect_deadline = time.monotonic() + 10.0
        while not converter.is_healthy and time.monotonic() < reconnect_deadline:
            await asyncio.sleep(0.02)
        assert converter.is_healthy, "converter did not reconnect"
        in_queue.put_nowait(_make_frame(3000))

        expected_count = pre_outage_count + 1
        while len(output_chunks) < expected_count and time.monotonic() < reconnect_deadline:
            await asyncio.sleep(0.05)

        assert len(output_chunks) == expected_count, (
            "stale reconnect survivors must not be emitted as converted output"
        )
        assert _decode_frame_value(output_chunks[-1]) == 3000
        assert converter.stale_input_drop_count == cap_frames
        print("Reconnect discarded all stale survivors and converted only fresh audio: OK")
    finally:
        if not server1_closed:
            server.close()
            await server.wait_closed()
        if server2 is not None:
            server2.close()
            await server2.wait_closed()
        await converter.close()
        await asyncio.wait_for(collect_task, timeout=5.0)
        await in_gen.aclose()

    print("RVCStreamingConverter buffer-cap drop-oldest test: SUCCESS")


async def test_rvc_streaming_adaptive_config_and_locked_pitch_resume():
    print("\n--- Testing RVCStreamingConverter adaptive-pitch config + locked_pitch resume ---")
    import json as _json

    converter = RVCStreamingConverter(
        ws_url="ws://127.0.0.1:1/ws",  # never actually connected in this test
        pitch_shift=7,
        adaptive_pitch=True,
        target_f0=208.0,
    )
    payload = _json.loads(converter._config_payload())
    assert payload["adaptive_pitch"] is True
    assert payload["target_f0"] == 208.0
    assert payload["pitch_shift"] == 7

    # Server reports the per-call lock via stats: the converter must adopt it so
    # the NEXT (reconnect) config resumes the locked identity instead of
    # re-adapting — the 2026-07-03 auto-detect revert was exactly about
    # re-detection on reconnect changing identity mid-call.
    await converter._handle_incoming(None, _json.dumps(
        {"type": "stats", "infer_ms": 55.0, "locked_pitch": 5.39}
    ))
    assert converter.pitch_shift == 5.39
    assert converter.adaptive_pitch is False
    payload = _json.loads(converter._config_payload())
    assert payload["pitch_shift"] == 5.39
    assert payload["adaptive_pitch"] is False

    # Non-adaptive converters ignore locked_pitch (defensive; server won't send it).
    fixed = RVCStreamingConverter(ws_url="ws://127.0.0.1:1/ws", pitch_shift=7)
    await fixed._handle_incoming(None, _json.dumps(
        {"type": "stats", "infer_ms": 55.0, "locked_pitch": 3.0}
    ))
    assert fixed.pitch_shift == 7
    print("RVCStreamingConverter adaptive config + locked_pitch resume: SUCCESS")


async def test_rvc_streaming_adaptive_locked_pitch_reconnect_e2e():
    print(
        "\n--- Testing RVCStreamingConverter e2e: locked_pitch resume survives a real "
        "WS reconnect (not just the _handle_incoming seam) ---"
    )

    received_configs = []
    connection_count = 0

    async def handler(websocket):
        nonlocal connection_count
        connection_count += 1
        conn_id = connection_count
        try:
            config_msg = await websocket.recv()  # the JSON config handshake message
            received_configs.append(json.loads(config_msg))
            await websocket.send(json.dumps({"type": "ready"}))
            if conn_id == 1:
                # Report the per-call lock via stats, then drop the connection --
                # proves the client's reconnect RESUMES this identity instead of
                # re-adapting (the whole point of reporting locked_pitch at all).
                await websocket.send(json.dumps(
                    {"type": "stats", "infer_ms": 1.0, "locked_pitch": 5.39}
                ))
                await asyncio.sleep(0.1)  # give the client time to process it
                await websocket.close()
                return
            # Second (reconnect) connection: nothing else under test here, just
            # keep the socket open until the test tears the converter down.
            async for message in websocket:
                pass
        except ConnectionClosed:
            pass

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        converter = RVCStreamingConverter(
            ws_url=f"ws://127.0.0.1:{port}/ws",
            pitch_shift=7,
            adaptive_pitch=True,
            target_f0=208.0,
        )
        in_gen, in_queue = _make_fed_input()
        output_chunks = []

        async def collect():
            async for chunk in converter.convert_stream(in_gen):
                output_chunks.append(chunk)

        collect_task = asyncio.create_task(collect())
        try:
            # Wait for the second (reconnect) config handshake -- that's the
            # proof a real WS reconnect happened, not just an in-memory field set.
            deadline = time.monotonic() + 5.0
            while len(received_configs) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert len(received_configs) >= 2, (
                f"expected a second (reconnect) config handshake, got {len(received_configs)}"
            )

            second_config = received_configs[1]
            assert second_config["pitch_shift"] == 5.39, (
                f"reconnect config did not resume the locked pitch: {second_config}"
            )
            assert second_config["adaptive_pitch"] is False, (
                f"reconnect config re-enabled adaptive pitch instead of resuming the lock: {second_config}"
            )
            print(
                "Reconnect config resumes locked identity "
                "(pitch_shift=5.39, adaptive_pitch=False): OK"
            )
        finally:
            await converter.close()
            await asyncio.wait_for(collect_task, timeout=5.0)
            await in_gen.aclose()

    print("RVCStreamingConverter e2e locked_pitch reconnect-resume test: SUCCESS")


async def test_worker_readiness_probe_dedup():
    print("\n--- Testing VoiceConversionWorker readiness-probe dedup (outbound race fix) ---")

    class _CountingReadyConverter:
        """Stands in for RVCStreamingConverter's wait_ready(): counts how many
        independent probe calls are made, so we can assert that starting the
        background probe (as _do_start_bot does) and then awaiting the shared
        result (as call_outbound now does) opens exactly ONE probe -- not two
        racing ones against a 1-concurrent-session backend."""

        def __init__(self):
            self.call_count = 0

        async def wait_ready(self, timeout):
            self.call_count += 1
            await asyncio.sleep(0.05)
            return True

    converter = _CountingReadyConverter()
    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=converter,
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )

    # Reproduce _do_start_bot's sequence: kick off the background probe, then
    # -- with no `await` in between, same as call_outbound -- have the caller
    # block on readiness via the new shared accessor rather than calling
    # wait_until_ready() directly (which would open a second probe session).
    worker.start_readiness_probe(5.0)
    ok = await worker.wait_for_readiness_probe(5.0)

    assert ok is True, "expected the shared readiness probe to report ready"
    assert converter.call_count == 1, (
        "expected exactly one wait_ready() probe call shared between the "
        f"background probe and the blocking waiter, got {converter.call_count}"
    )
    assert worker.is_ready is True, "is_ready should reflect the shared probe's result"
    print(f"wait_ready() probe calls for background+outbound paths: {converter.call_count} (expected 1) OK")

    # A worker with no background probe started (defensive/edge case, "shouldn't
    # happen given current wiring") must still resolve correctly rather than
    # hanging or crashing -- wait_for_readiness_probe() degrades to a direct
    # wait_until_ready() call in that case.
    converter2 = _CountingReadyConverter()
    worker2 = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=converter2,
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    ok2 = await worker2.wait_for_readiness_probe(5.0)
    assert ok2 is True
    assert converter2.call_count == 1
    print("wait_for_readiness_probe() with no background probe started degrades to a direct probe: OK")

    print("VoiceConversionWorker readiness-probe dedup test: SUCCESS")


async def test_on_converter_stats_accumulates_full_breakdown():
    print("\n--- Testing VoiceConversionWorker._on_converter_stats accumulation ---")

    class _StatsCapableConverter:
        """Only needs an on_stats attribute to exist for VoiceConversionWorker's
        hasattr() check to wire it up -- mirrors RVCStreamingConverter's shape
        without needing a real WS connection."""
        on_stats = None

    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=_StatsCapableConverter(),
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    assert worker._call_block_stats == [], "should start empty"

    # Before _run_conversion_stream ever runs, _playout_buffer doesn't exist yet --
    # playout_buffer_bytes must degrade to None rather than raising.
    worker._on_converter_stats({"infer_ms": 12.3, "block_ms": 320})
    assert worker._call_block_stats[-1]["playout_buffer_bytes"] is None, (
        "expected playout_buffer_bytes=None before the playout buffer is created"
    )

    # Once a playout buffer exists, its current length must be captured alongside
    # whatever fields the server sent (here, the full TRT stage breakdown).
    worker._playout_buffer = bytearray(b"\x00" * 4096)
    worker._on_converter_stats({
        "infer_ms": 58.1, "block_ms": 320, "lock_wait_ms": 3.5,
        "hubert_ms": 10.0, "index_ms": 1.0, "rmvpe_ms": 20.0,
        "generator_ms": 25.0, "postproc_ms": 2.1, "total_ms": 58.1,
    })

    assert len(worker._call_block_stats) == 2, f"expected 2 recorded blocks, got {len(worker._call_block_stats)}"
    second = worker._call_block_stats[1]
    assert second["playout_buffer_bytes"] == 4096, f"expected 4096, got {second['playout_buffer_bytes']}"
    assert second["hubert_ms"] == 10.0 and second["generator_ms"] == 25.0, (
        "expected the full server-reported stage breakdown to be preserved verbatim"
    )
    # infer_ms/block_ms latency-badge behavior must be untouched.
    assert worker._latest_latency_ms == 58.1 + 320, (
        f"existing pipeline_latency_ms computation regressed: {worker._latest_latency_ms}"
    )
    print(f"Accumulated {len(worker._call_block_stats)} block stats rows with playout_buffer_bytes: OK")
    print("_on_converter_stats accumulation test: SUCCESS")


async def test_log_call_latency_summary_prints_header_rows_and_aggregates():
    print("\n--- Testing _log_call_latency_summary output ---")
    import io
    import contextlib

    def make_worker():
        return VoiceConversionWorker(
            room_url="ws://unused",
            token="unused",
            converter=DummyVoiceConverter(),
            suppressor=WebRTCNoiseSuppressor(ns_level=3),
        )

    # No stats recorded at all: must not raise, must say so clearly.
    worker = make_worker()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        worker._log_call_latency_summary()
    assert "No converter stats recorded" in buf.getvalue()
    print("Empty-call case prints a clear no-data line: OK")

    # Populate synthetic per-block rows spanning the full stage breakdown.
    # Fresh worker: the summary is once-per-call (idempotence check below), so
    # reusing the one that already logged the empty-call line would print nothing.
    worker = make_worker()
    worker._call_block_stats = [
        {"infer_ms": 60.0, "block_ms": 320, "lock_wait_ms": 1.0, "hubert_ms": 10.0,
         "index_ms": 1.0, "rmvpe_ms": 20.0, "generator_ms": 27.0, "postproc_ms": 2.0,
         "total_ms": 60.0, "playout_buffer_bytes": 5000},
        {"infer_ms": 80.0, "block_ms": 320, "lock_wait_ms": 5.0, "hubert_ms": 12.0,
         "index_ms": 1.5, "rmvpe_ms": 25.0, "generator_ms": 39.0, "postproc_ms": 2.5,
         "total_ms": 80.0, "playout_buffer_bytes": 6000},
        {"infer_ms": 0.0, "block_ms": 320},  # a silence-bypassed block: no stage keys
    ]

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        worker._log_call_latency_summary()
    out = buf.getvalue()

    assert "3 block(s) this call" in out, "expected a header naming the block count"
    assert "block 0:" in out and "block 1:" in out and "block 2:" in out, (
        "expected one printed line per block"
    )
    assert "hubert_ms=10.0" in out, "expected raw per-block field values to be printed verbatim"
    # infer_ms aggregate across all 3 blocks (60, 80, 0): avg=46.67, median=60, max=80
    assert "infer_ms: avg=46.67 median=60.00 p95=80.00 max=80.00 n=3" in out, (
        f"unexpected infer_ms aggregate line, got:\n{out}"
    )
    # hubert_ms aggregate only over the 2 blocks that have it (silence-bypassed block excluded)
    assert "hubert_ms: avg=11.00 median=11.00 p95=12.00 max=12.00 n=2" in out, (
        f"unexpected hubert_ms aggregate line (should exclude the silence-bypassed block), got:\n{out}"
    )
    print("Populated-call case prints per-block rows and correct aggregates: OK")

    # stop() runs TWICE on the /api/call/end path (the endpoint calls it directly,
    # then run_worker_task's finally calls it again once worker.running goes false
    # -- backend/main.py:424-427 + 857-860). The summary must print once per call,
    # not once per stop() invocation.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        worker._log_call_latency_summary()
    assert buf.getvalue() == "", (
        "expected a second _log_call_latency_summary call to print nothing "
        f"(once-per-call idempotence guard), got:\n{buf.getvalue()}"
    )
    print("Second call on the same worker prints nothing (once per call): OK")
    print("_log_call_latency_summary test: SUCCESS")


async def test_stop_logs_summary_before_teardown():
    print("\n--- Testing stop() invokes the latency summary ---")

    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=DummyVoiceConverter(),
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    called = []
    worker._log_call_latency_summary = lambda: called.append(True)

    await worker.stop()

    assert called == [True], "expected stop() to call _log_call_latency_summary exactly once"
    print("stop() invokes _log_call_latency_summary: OK")
    print("stop() latency summary wiring test: SUCCESS")


async def test_playout_buffer_smooths_bursty_converter_output():
    print("\n--- Testing standing playout buffer (absorbs bursty/delayed converter output) ---")

    class _BurstyConverter:
        """Yields audio in an intentionally bursty pattern: a big delayed chunk
        after a gap, then several small on-time chunks -- shaped like the real
        GPU-behind-real-time symptom this buffer exists to absorb."""
        async def convert_stream(self, in_audio):
            # One big "late block" chunk (simulates a slow GPU block arriving
            # all at once) -- bigger than the test's target cushion below.
            yield b"\x00\x01" * 2000  # 4000 bytes
            await asyncio.sleep(0.01)
            for _ in range(5):
                yield b"\x00\x01" * 500  # 1000 bytes each
                await asyncio.sleep(0.01)

    converter = _BurstyConverter()
    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=converter,
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    # Bypass the real _publish_frames' internal 960-byte frame slicing
    # entirely -- this test cares about what the playout consumer hands to
    # _publish_frames in a single call (proving cushion-wait/batching), not
    # how those bytes get sliced into LiveKit frames downstream (unrelated,
    # unchanged behavior already exercised by production use).
    published_payloads = []

    async def fake_publish_frames(payload):
        published_payloads.append(len(payload))

    worker._publish_frames = fake_publish_frames
    # Small test-scale cushion/cap so the test runs fast and deterministically
    # -- same override pattern the buffer-cap test above uses on
    # RVCStreamingConverter's _MAX_BUFFER_BYTES, applied here to the new
    # playout buffer's class constants instead.
    worker._PLAYOUT_BUFFER_TARGET_BYTES = 3000
    worker._PLAYOUT_BUFFER_MAX_BYTES = 8000

    conversion_task = asyncio.create_task(worker._run_conversion_stream())
    try:
        deadline = time.monotonic() + 3.0
        total_expected = 4000 + 5 * 1000
        while sum(published_payloads) < total_expected and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

        published_total = sum(published_payloads)
        assert published_total == total_expected, (
            f"expected all {total_expected} converted bytes to eventually reach "
            f"_publish_frames (buffer must never silently drop data below its cap), "
            f"got {published_total}"
        )
        print(f"All {total_expected} bytes from a bursty converter reached _publish_frames: OK")

        # The first _publish_frames call must not happen until the target
        # cushion has accumulated -- proves this isn't just re-publishing each
        # chunk immediately as it arrives (that would be the old one-shot-only
        # behavior, not a standing buffer).
        assert published_payloads[0] >= worker._PLAYOUT_BUFFER_TARGET_BYTES, (
            "expected the first _publish_frames call to wait for the target cushion to fill, "
            f"got a first call of only {published_payloads[0]} bytes "
            f"(target was {worker._PLAYOUT_BUFFER_TARGET_BYTES})"
        )
        print(
            f"First _publish_frames call waited for the {worker._PLAYOUT_BUFFER_TARGET_BYTES}-byte "
            f"cushion (got {published_payloads[0]} bytes): OK"
        )
    finally:
        conversion_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await conversion_task

    print("Standing playout buffer test: SUCCESS")


async def test_playout_buffer_drops_oldest_over_cap():
    print("\n--- Testing standing playout buffer cap (bounded, drop-oldest) ---")

    class _SlowTrickleConverter:
        async def convert_stream(self, in_audio):
            for i in range(20):
                yield bytes([i % 256]) * 1000  # 1000 bytes per chunk, 20000 total
                await asyncio.sleep(0.01)

    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=_SlowTrickleConverter(),
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
    )
    # No audio_source needed: the target cushion below (50000 bytes) is set
    # deliberately higher than everything the converter ever yields (20000
    # bytes), so playout never starts and _publish_frames is never called --
    # this test is purely about the producer-side overflow/drop-oldest trim,
    # independent of how fast (or slow) playout itself is.
    worker._PLAYOUT_BUFFER_TARGET_BYTES = 50000  # higher than total fed: publish never starts
    worker._PLAYOUT_BUFFER_MAX_BYTES = 5000

    conversion_task = asyncio.create_task(worker._run_conversion_stream())
    try:
        await asyncio.sleep(0.5)  # let all 20 chunks (20000 bytes) arrive and overflow the 5000 cap
        assert len(worker._playout_buffer) == worker._PLAYOUT_BUFFER_MAX_BYTES, (
            f"expected playout buffer to sit exactly at the {worker._PLAYOUT_BUFFER_MAX_BYTES}-byte "
            f"cap, got {len(worker._playout_buffer)}"
        )
        # Drop-oldest means the buffer's tail must be the newest bytes fed
        # (value 19, the last chunk's fill byte), not the oldest (value 0).
        assert worker._playout_buffer[-1] == 19, (
            f"expected the newest fed byte (19) to survive at the tail, got {worker._playout_buffer[-1]}"
        )
        assert worker._playout_buffer[0] != 0, (
            "expected the oldest fed bytes (value 0) to have been dropped, but they're still at the head"
        )
        print(
            f"Playout buffer capped at {worker._PLAYOUT_BUFFER_MAX_BYTES} bytes, "
            "oldest bytes dropped, newest survive: OK"
        )
    finally:
        conversion_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await conversion_task

    print("Standing playout buffer cap/drop-oldest test: SUCCESS")


async def test_presence_eq():
    print("\n--- Testing Presence EQ ---")
    from backend.audio_eq import PresenceEQ

    sr = 48000
    dur = 1.0
    t = np.arange(int(sr * dur)) / sr

    def band_gain_db(freq: float) -> float:
        """Feed a pure tone through a fresh EQ and measure steady-state gain."""
        eq = PresenceEQ(gain_db=4.0, sample_rate=sr)
        tone = (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)
        out = np.frombuffer(eq.process(tone.tobytes()), dtype=np.int16)
        assert len(out) == len(tone), f"length changed: {len(tone)} -> {len(out)}"
        # skip the filter's warm-up tail at the start
        skip = 2000
        in_rms = np.sqrt(np.mean(tone[skip:].astype(np.float64) ** 2))
        out_rms = np.sqrt(np.mean(out[skip:].astype(np.float64) ** 2))
        return 20 * np.log10(out_rms / in_rms)

    g_low = band_gain_db(300.0)
    g_mid = band_gain_db(2200.0)
    print(f"gain @300Hz: {g_low:+.2f} dB, gain @2.2kHz: {g_mid:+.2f} dB")
    assert abs(g_low) < 1.0, f"300Hz band should pass ~unchanged, got {g_low:+.2f} dB"
    assert abs(g_mid - 4.0) < 1.0, f"2.2kHz band should gain ~+4 dB, got {g_mid:+.2f} dB"

    # Streaming continuity: processing in arbitrary chunk sizes must produce
    # byte-identical output to a single pass — no clicks at chunk boundaries.
    rng = np.random.default_rng(7)
    signal = (rng.standard_normal(sr) * 4000).clip(-32768, 32767).astype(np.int16)
    single = PresenceEQ(gain_db=4.0, sample_rate=sr).process(signal.tobytes())
    eq = PresenceEQ(gain_db=4.0, sample_rate=sr)
    chunks, pos = [], 0
    while pos < len(signal):
        n = int(rng.integers(1, 4000))
        chunks.append(eq.process(signal[pos:pos + n].tobytes()))
        pos += n
    chunked = b"".join(chunks)
    assert chunked == single, "chunked output differs from single-pass — boundary discontinuity"

    # Loud input must not wrap around when boosted — it must clip safely.
    loud = np.full(sr // 10, 30000, dtype=np.int16)
    out = np.frombuffer(PresenceEQ(gain_db=4.0, sample_rate=sr).process(loud.tobytes()), dtype=np.int16)
    assert out.max() <= 32767 and out.min() >= -32768
    print("Presence EQ Test: SUCCESS")


async def test_worker_applies_presence_eq():
    print("\n--- Testing worker applies Presence EQ to converted output ---")
    import os
    from backend.audio_eq import PresenceEQ

    t = np.arange(9600) / 48000.0  # 200ms @ 48kHz
    tone_bytes = (np.sin(2 * np.pi * 2200 * t) * 8000).astype(np.int16).tobytes()

    class _OneChunkConverter:
        async def convert_stream(self, in_audio):
            yield tone_bytes
            # Keep the duplex stream open (like a real call) so
            # _run_conversion_stream doesn't tear down the playout consumer
            # before it has drained the buffer; the test cancels us.
            await asyncio.sleep(30)

    async def run_worker_once() -> bytes:
        worker = VoiceConversionWorker(
            room_url="ws://unused",
            token="unused",
            converter=_OneChunkConverter(),
            suppressor=WebRTCNoiseSuppressor(ns_level=3),
        )
        published = []

        async def fake_publish_frames(payload):
            published.append(bytes(payload))

        worker._publish_frames = fake_publish_frames
        worker._PLAYOUT_BUFFER_TARGET_BYTES = 1000  # publish promptly in test
        task = asyncio.create_task(worker._run_conversion_stream())
        try:
            deadline = time.monotonic() + 3.0
            while sum(len(p) for p in published) < len(tone_bytes) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return b"".join(published)

    # Default: EQ on, output must match PresenceEQ with the default gain.
    os.environ.pop("PRESENCE_EQ_GAIN_DB", None)
    got = await run_worker_once()
    expected = PresenceEQ(gain_db=4.0, sample_rate=48000).process(tone_bytes)
    assert got == expected, "worker output should be the presence-EQ'd converted audio"
    assert got != tone_bytes, "EQ'd output should differ from the raw converted audio"
    print("Default: converted audio is presence-EQ'd before publish: OK")

    # PRESENCE_EQ_GAIN_DB=0 disables the EQ: bytes pass through untouched.
    os.environ["PRESENCE_EQ_GAIN_DB"] = "0"
    try:
        got = await run_worker_once()
        assert got == tone_bytes, "gain 0 must bypass the EQ entirely"
    finally:
        os.environ.pop("PRESENCE_EQ_GAIN_DB", None)
    print("PRESENCE_EQ_GAIN_DB=0 bypasses the EQ: OK")
    print("Worker Presence EQ Wiring Test: SUCCESS")


async def test_bounded_audio_queue():
    print("\n--- Testing Bounded Audio Queue ---")
    from backend.pipeline import BoundedAudioQueue
    
    q = BoundedAudioQueue(maxsize=3)
    assert q.empty()
    assert q.qsize() == 0
    
    await q.put(1)
    await q.put(2)
    await q.put(3)
    assert q.qsize() == 3
    assert not q.empty()
    
    # Put another item, which should trigger a drop of the oldest (1)
    await q.put(4)
    assert q.qsize() == 3
    assert q.drop_count == 1
    
    val1 = await q.get()
    q.task_done()
    assert val1 == 2, f"Expected 2 (oldest item 1 should be dropped), got {val1}"
    
    val2 = await q.get()
    q.task_done()
    assert val2 == 3, f"Expected 3, got {val2}"
    
    val3 = await q.get()
    q.task_done()
    assert val3 == 4, f"Expected 4, got {val3}"
    assert q.empty()
    print("Bounded Audio Queue Test: SUCCESS")


async def test_worker_telemetry_publish():
    print("\n--- Testing VoiceConversionWorker Telemetry & Shared Contracts ---")
    
    class _DummyConverterWithStats:
        on_stats = None

    converter = _DummyConverterWithStats()
    worker = VoiceConversionWorker(
        room_url="ws://unused",
        token="unused",
        converter=converter,
        suppressor=WebRTCNoiseSuppressor(ns_level=3),
        call_id="test-call-123",
        requested_engine="llvc",
        effective_engine="rvc",
        fallback_reason="LLVC unready",
    )
    
    assert worker.call_id == "test-call-123"
    assert worker.requested_engine == "llvc"
    assert worker.effective_engine == "rvc"
    assert worker.fallback_reason == "LLVC unready"
    
    # Mock room and local participant for data publishing
    mock_participant = AsyncMock()
    mock_room = MagicMock()
    mock_room.isconnected.return_value = True
    mock_room.local_participant = mock_participant
    worker.room = mock_room
    
    # Trigger stats message and verify stats updates the values
    worker._playout_buffer = bytearray(b"\x00" * 960) # 10 ms at 48kHz mono 16-bit
    worker._on_converter_stats({
        "infer_ms": 35.0,
        "block_ms": 320,
        "converter_wait_ms": 50.0,
        "network_rtt_ms": 15.0,
    })
    
    # Wait a bit for the async task in _publish_latency_metric to run
    await asyncio.sleep(0.05)
    
    assert mock_participant.publish_data.called
    payload_bytes = mock_participant.publish_data.call_args[0][0]
    payload = json.loads(payload_bytes.decode())
    
    assert payload["call_id"] == "test-call-123"
    assert payload["requested_engine"] == "llvc"
    assert payload["effective_engine"] == "rvc"
    assert payload["fallback_reason"] == "LLVC unready"
    assert payload["converter_wait_ms"] == 50.0
    assert payload["network_rtt_ms"] == 15.0
    assert payload["inference_ms"] == 35.0
    assert payload["playout_buffer_latency_ms"] == 10.0
    
    print("VoiceConversionWorker Telemetry & Shared Contracts: SUCCESS")


def test_llvc_stateful_upsampler():
    print("\n--- Testing LLVC Stateful Upsampler ---")
    from backend.converters.llvc_fake_server import StatefulUpsampler
    
    upsampler = StatefulUpsampler()
    # Feed frame 1: [10, 20]
    frame1 = np.array([10, 20], dtype=np.int16).tobytes()
    res1 = upsampler.process(frame1)
    samples1 = np.frombuffer(res1, dtype=np.int16)
    # Expected: prev was 0, next is [10, 20]
    # out[0] = 0
    # out[1] = (2*0 + 10)//3 = 3
    # out[2] = (0 + 2*10)//3 = 6
    # out[3] = 10
    # out[4] = (2*10 + 20)//3 = 13
    # out[5] = (10 + 2*20)//3 = 16
    assert np.array_equal(samples1, [0, 3, 6, 10, 13, 16])
    
    # Feed frame 2: [30, 40]
    frame2 = np.array([30, 40], dtype=np.int16).tobytes()
    res2 = upsampler.process(frame2)
    samples2 = np.frombuffer(res2, dtype=np.int16)
    # Expected: prev was 20, next is [30, 40]
    # out[0] = 20
    # out[1] = (2*20 + 30)//3 = 23
    # out[2] = (20 + 2*30)//3 = 26
    # out[3] = 30
    # out[4] = (2*30 + 40)//3 = 33
    # out[5] = (30 + 2*40)//3 = 36
    assert np.array_equal(samples2, [20, 23, 26, 30, 33, 36])
    print("LLVC Stateful Upsampler Test: SUCCESS")


def test_llvc_stateful_causal_model():
    print("\n--- Testing LLVC Stateful Causal Model (Ring Modulator) ---")
    from backend.converters.llvc_fake_server import StatefulCausalModel
    
    model = StatefulCausalModel(carrier_freq=250.0, sample_rate=48000.0)
    assert model.phase == 0.0
    
    # Process block 1
    samples1 = np.full(100, 1000, dtype=np.int16).tobytes()
    res1 = model.process(samples1)
    
    # Process block 2 - phase should carry over
    res2 = model.process(samples1)
    
    # Verify phase is within 0..2*pi
    assert 0.0 <= model.phase < 2.0 * np.pi
    
    # Check that output is not just zeros or unmodulated
    samples_out1 = np.frombuffer(res1, dtype=np.int16)
    samples_out2 = np.frombuffer(res2, dtype=np.int16)
    assert not np.array_equal(samples_out1, np.full(100, 1000, dtype=np.int16))
    assert not np.array_equal(samples_out2, np.full(100, 1000, dtype=np.int16))
    
    print("LLVC Stateful Causal Model Test: SUCCESS")


async def test_llvc_streaming_converter_basic():
    print("\n--- Testing LLVCStreamingConverter handshake, echo, and close() ---")
    from backend.converters.llvc_fake_server import llvc_fake_ws_handler
    from backend.converters.llvc_stream import LLVCStreamingConverter
    
    server = await websockets.serve(llvc_fake_ws_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    
    converter = LLVCStreamingConverter(ws_url=f"ws://127.0.0.1:{port}/ws", api_key="secret-key")
    in_gen, in_queue = _make_fed_input()
    output_chunks = []
    
    async def collect():
        async for chunk in converter.convert_stream(in_gen):
            output_chunks.append(chunk)
            
    collect_task = asyncio.create_task(collect())
    
    # Feed 1 frame (320 bytes = 160 samples of 1000)
    in_queue.put_nowait(np.full(160, 1000, dtype=np.int16).tobytes())
    
    deadline = time.monotonic() + 3.0
    while len(output_chunks) < 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
        
    assert len(output_chunks) >= 1
    out_samples = np.frombuffer(output_chunks[0], dtype=np.int16)
    assert len(out_samples) == 480 # 3x upsampling of 160
    
    await converter.close()
    await asyncio.wait_for(collect_task, timeout=2.0)
    server.close()
    await server.wait_closed()
    print("LLVCStreamingConverter basic test: SUCCESS")


async def test_llvc_streaming_converter_reconnect():
    print("\n--- Testing LLVCStreamingConverter reconnect and buffer cap ---")
    from backend.converters.llvc_fake_server import llvc_fake_ws_handler
    from backend.converters.llvc_stream import LLVCStreamingConverter
    
    active_websockets = []
    async def custom_handler(websocket):
        active_websockets.append(websocket)
        try:
            await llvc_fake_ws_handler(websocket)
        finally:
            if websocket in active_websockets:
                active_websockets.remove(websocket)
                
    server = await websockets.serve(custom_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    
    converter = LLVCStreamingConverter(ws_url=f"ws://127.0.0.1:{port}/ws")
    in_gen, in_queue = _make_fed_input()
    output_chunks = []
    
    async def collect():
        async for chunk in converter.convert_stream(in_gen):
            output_chunks.append(chunk)
            
    collect_task = asyncio.create_task(collect())
    
    # First connection works
    in_queue.put_nowait(_make_frame(10))
    deadline = time.monotonic() + 3.0
    while len(output_chunks) < 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert len(output_chunks) == 1
    
    # Outage
    server.close()
    await server.wait_closed()
    for ws in list(active_websockets):
        await ws.close()
        
    await asyncio.sleep(0.6)
    
    # Feed 100 frames (above 50 cap)
    for i in range(100):
        in_queue.put_nowait(_make_frame(i))
        
    # Wait for drain
    await asyncio.sleep(0.5)
    
    assert converter.drop_count == 50
    assert len(converter._buffer) == 50
    
    # Reconnect. The retained 50 frames are older than the live-edge cap and
    # must be discarded before send; only a fresh post-handshake frame returns.
    server2 = await websockets.serve(llvc_fake_ws_handler, "127.0.0.1", port)

    deadline = time.monotonic() + 10.0
    while not converter.is_healthy and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert converter.is_healthy
    in_queue.put_nowait(_make_frame(1000))

    expected_count = 2
    while len(output_chunks) < expected_count and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    assert len(output_chunks) == expected_count
    assert converter.stale_input_drop_count == 50
    assert _decode_frame_value(output_chunks[-1]) != 1000, (
        "LLVC fake server must return converted, not representative raw audio"
    )
    
    await converter.close()
    await asyncio.wait_for(collect_task, timeout=2.0)
    server2.close()
    await server2.wait_closed()
    print("LLVCStreamingConverter reconnect and buffer cap test: SUCCESS")


async def test_llvc_concurrency_limit():
    print("\n--- Testing LLVC Fake Server Concurrency Limit (max 2) ---")
    from backend.converters.llvc_fake_server import llvc_fake_ws_handler
    from backend.converters.llvc_stream import LLVCStreamingConverter
    
    server = await websockets.serve(llvc_fake_ws_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    
    # Client 1
    c1 = LLVCStreamingConverter(ws_url=f"ws://127.0.0.1:{port}/ws")
    g1, q1 = _make_fed_input()
    task1 = asyncio.create_task(anext(c1.convert_stream(g1)))
    q1.put_nowait(_make_frame(1))
    
    # Client 2
    c2 = LLVCStreamingConverter(ws_url=f"ws://127.0.0.1:{port}/ws")
    g2, q2 = _make_fed_input()
    task2 = asyncio.create_task(anext(c2.convert_stream(g2)))
    q2.put_nowait(_make_frame(2))
    
    # Give them a moment to connect
    await asyncio.sleep(0.2)
    
    # Client 3 should be rejected with busy
    c3 = LLVCStreamingConverter(ws_url=f"ws://127.0.0.1:{port}/ws")
    g3, q3 = _make_fed_input()
    
    # We capture logs to verify warning
    with _capture_logs("backend.converters.llvc_stream") as log_capture:
        task3 = asyncio.create_task(anext(c3.convert_stream(g3)))
        q3.put_nowait(_make_frame(3))
        await asyncio.sleep(0.5)
        
        # Check logs for "busy"
        busy_logged = any("server busy" in r.getMessage() for r in log_capture.records)
        assert busy_logged, "Expected server busy warning in logs"
        
    await c1.close()
    await c2.close()
    await c3.close()
    
    # Cleanup task exceptions
    for t in [task1, task2, task3]:
        with contextlib.suppress(Exception):
            await t
            
    server.close()
    await server.wait_closed()
    print("LLVC Concurrency Limit Test: SUCCESS")


async def test_llvc_pre_call_fallback():
    print("\n--- Testing LLVC Pre-Call Fallback to RVC ---")
    from backend.converters.llvc_stream import LLVCStreamingConverter
    converter = LLVCStreamingConverter(ws_url="ws://127.0.0.1:9999/ws")
    res = await converter.wait_ready(0.1)
    assert res is False, "wait_ready should fail on closed port"
    print("LLVC Pre-Call Fallback test: SUCCESS")


async def test_llvc_mid_call_watchdog():
    print("\n--- Testing LLVC Mid-Call Watchdog (2.0s outage) ---")
    class DummyConverter:
        is_healthy = False

        async def convert_stream(self, in_audio):
            while True:
                await asyncio.sleep(10.0)
                yield b""
                
    worker = VoiceConversionWorker(
        room_url="ws://localhost",
        token="token",
        converter=DummyConverter(),
        suppressor=None,
        requested_engine="llvc",
        effective_engine="llvc",
    )
    
    fatal_called = asyncio.Event()
    async def on_fatal():
        fatal_called.set()
        
    worker.on_llvc_fatal_failure = on_fatal
    worker._last_chunk_at = time.monotonic()
    worker._last_submitted_input_at = time.monotonic()
    worker._last_converted_output_at = 0.0
    
    watchdog = asyncio.create_task(worker._holding_watchdog())
    try:
        await asyncio.wait_for(fatal_called.wait(), timeout=3.0)
    finally:
        watchdog.cancel()
        
    assert fatal_called.is_set()
    print("LLVC Mid-Call Watchdog test: SUCCESS")


async def main():
    print("Running automated pipeline verification tests...")
    await test_bounded_audio_queue()
    await test_worker_telemetry_publish()
    await test_noise_suppressor()
    await test_dummy_converter()
    await test_rvc_converter_mocked()
    await test_rvc_converter_empty_response()
    await test_rvc_streaming_converter_basic()
    await test_rvc_streaming_converter_reconnect()
    await test_rvc_streaming_converter_buffer_cap_drop_oldest()
    await test_rvc_streaming_adaptive_config_and_locked_pitch_resume()
    await test_rvc_streaming_adaptive_locked_pitch_reconnect_e2e()
    await test_worker_readiness_probe_dedup()
    await test_on_converter_stats_accumulates_full_breakdown()
    await test_log_call_latency_summary_prints_header_rows_and_aggregates()
    await test_stop_logs_summary_before_teardown()
    await test_playout_buffer_smooths_bursty_converter_output()
    await test_playout_buffer_drops_oldest_over_cap()
    await test_presence_eq()
    await test_worker_applies_presence_eq()
    
    # LLVC tests
    test_llvc_stateful_upsampler()
    test_llvc_stateful_causal_model()
    await test_llvc_streaming_converter_basic()
    await test_llvc_streaming_converter_reconnect()
    await test_llvc_concurrency_limit()
    await test_llvc_pre_call_fallback()
    await test_llvc_mid_call_watchdog()
    print("\nAll automated verification tests completed successfully!")

if __name__ == "__main__":
    asyncio.run(main())
