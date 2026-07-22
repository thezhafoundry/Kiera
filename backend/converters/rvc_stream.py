import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import AsyncIterator, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .base import VoiceConverter
from .streaming_safety import (
    BufferedInput,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    DEFAULT_HEARTBEAT_TIMEOUT_S,
    DEFAULT_MAX_MESSAGE_SIZE,
    DEFAULT_OUTPUT_QUEUE_MAX_CHUNKS,
    FatalOutput,
    LIVE_EDGE_MAX_AGE_S,
    validate_streaming_endpoint,
)

logger = logging.getLogger(__name__)

# Bytes/second of raw 16kHz mono 16-bit PCM input (client -> server direction).
_INPUT_BYTES_PER_SECOND = 16000 * 2
# Hold-don't-leak reconnect buffer cap: 500ms of buffered *input* audio. Beyond this,
# oldest frames are dropped (never raw audio to the lead — this only affects what
# gets converted once the socket reconnects).
_MAX_BUFFER_BYTES = int(_INPUT_BYTES_PER_SECOND * 0.5)

_BACKOFF_INITIAL_S = 0.5
_BACKOFF_MAX_S = 5.0

# Sentinel pushed onto the output queue to signal the generator should stop.
_CLOSE_SENTINEL = object()


def derive_ws_url(endpoint_url: str) -> str:
    """Derives the /ws streaming URL from an HTTP(S) /convert endpoint URL, mirroring
    the health-URL derivation in main.py's _wait_for_rvc_ready: swap the scheme
    (https->wss, http->ws) and the path/name variant (/convert -> /ws, and the
    web-convert / web_convert Modal function-name variants -> web-ws / web_ws)."""
    url = endpoint_url.strip()
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    url = (
        url.replace("/convert", "/ws")
        .replace("web-convert", "web-ws")
        .replace("web_convert", "web_ws")
    )
    return url


class RVCStreamingConverter(VoiceConverter):
    """
    Voice converter that streams audio to the Modal RVC `/ws` persistent-session
    endpoint (see modal_deploy/worker.py) instead of issuing one HTTP request per
    chunk. Input is raw 16kHz mono int16 PCM; output is 48kHz mono int16 PCM,
    yielded as the server produces it.

    NEVER yields raw/unconverted audio. If the socket drops mid-call, incoming
    input frames are buffered (bounded) while the converter reconnects; the
    output side simply pauses (silence) for that duration.
    """

    def __init__(
        self,
        endpoint_url: str = "",
        ws_url: str = "",
        api_key: str = "",
        pitch_shift: float = -1,
        index_rate: float = 0.75,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
        connect_timeout: float = 10.0,
        adaptive_pitch: bool = False,
        target_f0: float = 208.0,
        output_queue_max_chunks: int = DEFAULT_OUTPUT_QUEUE_MAX_CHUNKS,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
        model_version: str = "unknown",
    ):
        self.ws_url = (
            ws_url
            or os.getenv("RVC_WS_URL", "")
            or derive_ws_url(endpoint_url or os.getenv("RVC_ENDPOINT_URL", ""))
        ).rstrip("/")
        self.api_key = api_key or ""
        validate_streaming_endpoint(self.ws_url, self.api_key)
        self.pitch_shift = pitch_shift
        self.adaptive_pitch = adaptive_pitch
        self.target_f0 = target_f0
        self.index_rate = index_rate
        self.rms_mix_rate = rms_mix_rate
        self.protect = protect
        self.connect_timeout = connect_timeout
        self.output_queue_max_chunks = max(1, int(output_queue_max_chunks))
        self.max_message_size = max(1, int(max_message_size))
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.drop_count = 0
        self.stale_input_drop_count = 0
        self.input_overflow_drop_count = 0
        self.output_drop_count = 0
        self.connection_failure_count = 0
        self.model_version = model_version or "unknown"
        self.profile = "unknown"
        self.block_ms = 320
        self.context_ms = 400
        self.sola_ms = 80
        self.sample_rate_in = 16000
        self.sample_rate_out = 48000
        self.use_trt = False
        self._block_size_bytes = self.sample_rate_in * 2 * self.block_ms // 1000
        self._block_sent_timestamps = deque()
        self._sent_bytes_count = 0
        self._block_started_at: Optional[float] = None
        self._stats_sequence = 0

        # Optional callback: on_stats(dict) — called with the server's raw
        # {"type":"stats","infer_ms":...,"block_ms":...} payload (minus "type").
        self.on_stats: Optional[Callable[[dict], None]] = None

        # ---- long-lived duplex state (set up in convert_stream) ----
        self._buffer: deque = deque()
        self._buffered_bytes = 0
        self._buffer_lock: Optional[asyncio.Lock] = None
        self._buffer_not_empty: Optional[asyncio.Event] = None
        self._input_exhausted = False
        self._closed = False
        self._out_queue: Optional[asyncio.Queue] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._conn_task: Optional[asyncio.Task] = None
        self._is_healthy = False
        self._fatal_error: Optional[RuntimeError] = None
        self._stream_ready = asyncio.Event()

    @property
    def is_healthy(self) -> bool:
        return self._is_healthy and not self._closed

    def _mark_connection_lost(self, reason: str) -> None:
        was_healthy = self._is_healthy
        self._is_healthy = False
        if was_healthy:
            self.connection_failure_count += 1
            logger.warning(
                "[RVCStreamingConverter] active session became unhealthy: %s",
                reason,
            )

    def _config_payload(self) -> str:
        return json.dumps({
            "type": "config",
            "pitch_shift": self.pitch_shift,
            "index_rate": self.index_rate,
            "rms_mix_rate": self.rms_mix_rate,
            "protect": self.protect,
            "adaptive_pitch": self.adaptive_pitch,
            "target_f0": self.target_f0,
        })

    def _connect_kwargs(self) -> dict:
        kwargs = {
            "open_timeout": self.connect_timeout,
            "max_size": self.max_message_size,
            "ping_interval": self.heartbeat_interval,
            "ping_timeout": self.heartbeat_timeout,
        }
        if self.api_key:
            kwargs["additional_headers"] = {"Authorization": f"Bearer {self.api_key}"}
        return kwargs

    def _apply_ready_metadata(self, data: dict) -> None:
        """Apply effective server geometry from a readiness handshake."""
        self.model_version = str(data.get("model_version") or self.model_version)
        self.profile = str(data.get("profile") or self.profile)

        for key in (
            "block_ms",
            "context_ms",
            "sola_ms",
            "sample_rate_in",
            "sample_rate_out",
        ):
            try:
                value = int(data.get(key, getattr(self, key)))
            except (TypeError, ValueError):
                continue
            if value > 0:
                setattr(self, key, value)

        if isinstance(data.get("use_trt"), bool):
            self.use_trt = data["use_trt"]

        self._block_size_bytes = self.sample_rate_in * 2 * self.block_ms // 1000
        self._sent_bytes_count = 0
        self._block_started_at = None
        self._block_sent_timestamps.clear()

    # ------------------------------------------------------------------
    # Warm-gate probe (Task 4): a short-lived connection that just confirms
    # the server can hand back {"type":"ready"} within `timeout`.
    # ------------------------------------------------------------------
    async def wait_ready(self, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                async with websockets.connect(self.ws_url, **self._connect_kwargs()) as ws:
                    await ws.send(self._config_payload())
                    while True:
                        reply = await ws.recv()
                        if isinstance(reply, (bytes, bytearray)):
                            continue  # not expected before "ready"; ignore
                        data = json.loads(reply)
                        msg_type = data.get("type")
                        if msg_type == "ready":
                            self._apply_ready_metadata(data)
                            return True
                        if msg_type in ("busy", "error"):
                            return False
                        # ignore anything else (e.g. stray "pong") and keep waiting
        except Exception as e:
            logger.warning("[RVCStreamingConverter] wait_ready failed: %s", e)
            return False

    async def wait_stream_ready(self, timeout: float) -> bool:
        """Wait for the handshake on the active long-lived stream."""
        try:
            async with asyncio.timeout(timeout):
                await self._stream_ready.wait()
            return self.is_healthy
        except Exception as e:
            logger.warning("[RVCStreamingConverter] active stream readiness failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Long-lived duplex conversion
    # ------------------------------------------------------------------
    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        self._buffer = deque()
        self._buffered_bytes = 0
        self._buffer_lock = asyncio.Lock()
        self._buffer_not_empty = asyncio.Event()
        self._input_exhausted = False
        self._closed = False
        self._out_queue = asyncio.Queue(maxsize=self.output_queue_max_chunks)
        self._sent_bytes_count = 0
        self._block_started_at = None
        self._block_sent_timestamps = deque()
        self._stats_sequence = 0
        self.drop_count = 0
        self.stale_input_drop_count = 0
        self.input_overflow_drop_count = 0
        self.output_drop_count = 0
        self.connection_failure_count = 0
        self._is_healthy = False
        self._fatal_error = None
        self._stream_ready.clear()

        self._pump_task = asyncio.create_task(self._pump_input(in_audio))
        self._conn_task = asyncio.create_task(self._connection_loop())

        try:
            while True:
                item = await self._next_output()
                if item is _CLOSE_SENTINEL:
                    break
                yield item
        finally:
            await self._teardown()

    async def _teardown(self):
        self._closed = True
        self._is_healthy = False
        self._stream_ready.set()
        for task in (self._pump_task, self._conn_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._pump_task, self._conn_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass  # expected on teardown — not an error
                except Exception:
                    # A real fault in a background task (e.g. the in_audio iterator
                    # raised) winds the call down to silence — log so it's traceable.
                    logger.exception("[RVCStreamingConverter] background task failed during teardown")

    async def close(self) -> None:
        """Stop streaming and release the connection. Safe to call even if
        convert_stream's generator is still being iterated elsewhere — that
        loop will observe the sentinel/cancellation and stop cleanly."""
        self._closed = True
        self._is_healthy = False
        if self._out_queue is not None:
            self._put_control(_CLOSE_SENTINEL)
        await self._teardown()

    async def _next_output(self):
        item = await self._out_queue.get()
        if isinstance(item, FatalOutput):
            raise item.error
        return item

    def _put_control(self, item) -> None:
        if self._out_queue is None:
            return
        if item is _CLOSE_SENTINEL and self._fatal_error is not None:
            # FatalOutput has priority over normal closure. In a one-slot queue,
            # replacing it with the close sentinel would turn a fatal error into
            # clean exhaustion and make call cleanup nondeterministic.
            return
        while self._out_queue.full():
            self._out_queue.get_nowait()
        self._out_queue.put_nowait(item)

    async def _enqueue_output(self, chunk: bytes) -> None:
        if self._out_queue is None or self._closed or self._fatal_error is not None:
            return
        if self._out_queue.full():
            self._out_queue.get_nowait()
            self.output_drop_count += 1
            logger.warning(
                "[RVCStreamingConverter] converted output queue full — "
                "dropped oldest chunk"
            )
        self._out_queue.put_nowait(chunk)

    async def _signal_fatal(self, message: str) -> None:
        self._is_healthy = False
        self._closed = True
        self._stream_ready.set()
        self._fatal_error = RuntimeError(
            f"RVC streaming converter fatal error: {message}"
        )
        self._put_control(FatalOutput(self._fatal_error))

    # ---- input pump: continuously drains in_audio into the bounded buffer ----
    async def _pump_input(self, in_audio: AsyncIterator[bytes]):
        try:
            async for frame in in_audio:
                if self._closed:
                    break
                await self._buffer_input(frame)
        except asyncio.CancelledError:
            raise
        finally:
            self._input_exhausted = True
            self._buffer_not_empty.set()

    async def _buffer_input(
        self,
        frame: bytes,
        *,
        enqueued_at: Optional[float] = None,
    ) -> None:
        buffered = BufferedInput(
            enqueued_at=time.monotonic() if enqueued_at is None else enqueued_at,
            payload=bytes(frame),
        )
        async with self._buffer_lock:
            self._buffer.append(buffered)
            self._buffered_bytes += len(buffered.payload)
            while self._buffered_bytes > _MAX_BUFFER_BYTES and self._buffer:
                dropped = self._buffer.popleft()
                self._buffered_bytes -= len(dropped.payload)
                self.drop_count += 1
                self.input_overflow_drop_count += 1
                logger.warning(
                    "[RVCStreamingConverter] reconnect buffer full (500ms cap) — "
                    "dropped oldest input frame (%d bytes)",
                    len(dropped.payload),
                )
        self._buffer_not_empty.set()

    async def _requeue_input(self, buffered: BufferedInput) -> None:
        """Requeue a failed send without exceeding the 500ms live edge/cap."""
        async with self._buffer_lock:
            if time.monotonic() - buffered.enqueued_at > LIVE_EDGE_MAX_AGE_S:
                self.drop_count += 1
                self.stale_input_drop_count += 1
                logger.warning(
                    "[RVCStreamingConverter] failed-send frame became stale — dropped"
                )
                return

            self._buffer.appendleft(buffered)
            self._buffered_bytes += len(buffered.payload)
            while self._buffered_bytes > _MAX_BUFFER_BYTES and self._buffer:
                dropped = self._buffer.popleft()
                self._buffered_bytes -= len(dropped.payload)
                self.drop_count += 1
                self.input_overflow_drop_count += 1
                logger.warning(
                    "[RVCStreamingConverter] failed-send requeue exceeded 500ms cap — "
                    "dropped oldest input frame (%d bytes)",
                    len(dropped.payload),
                )
            if self._buffer:
                self._buffer_not_empty.set()

    def _apply_stats_sequence(self, data: dict) -> None:
        next_sequence = self._stats_sequence + 1
        try:
            server_sequence = int(data.get("sequence_id", next_sequence))
        except (TypeError, ValueError):
            server_sequence = next_sequence
        self._stats_sequence = max(next_sequence, server_sequence)
        data["sequence_id"] = self._stats_sequence

    # ---- connection manager: connect/handshake, reconnect with backoff, and
    # run the send/receive loop for as long as the socket stays up ----
    async def _connection_loop(self):
        backoff = _BACKOFF_INITIAL_S
        try:
            while not self._closed:
                try:
                    async with websockets.connect(self.ws_url, **self._connect_kwargs()) as ws:
                        await ws.send(self._config_payload())
                        handshake = await asyncio.wait_for(ws.recv(), timeout=self.connect_timeout)
                        if isinstance(handshake, (bytes, bytearray)):
                            raise WebSocketException("unexpected binary handshake reply")
                        data = json.loads(handshake)
                        if data.get("type") == "busy":
                            raise WebSocketException("server session busy")
                        if data.get("type") == "error" and data.get("fatal"):
                            await self._signal_fatal(
                                str(data.get("message") or "server rejected session")
                            )
                            return
                        if data.get("type") != "ready":
                            raise WebSocketException(f"unexpected handshake reply: {data}")

                        backoff = _BACKOFF_INITIAL_S  # reset after a successful handshake
                        self._apply_ready_metadata(data)
                        self._is_healthy = True
                        self._stream_ready.set()

                        writer_task = asyncio.create_task(self._writer_loop(ws))
                        try:
                            async for message in ws:
                                await self._handle_incoming(ws, message)
                        finally:
                            self._mark_connection_lost("reader or writer stopped")
                            writer_task.cancel()
                            try:
                                await writer_task
                            except asyncio.CancelledError:
                                pass

                        # Reader loop only ends when the socket closes (or we're
                        # shutting down); either way fall through to the outer
                        # while-loop, which reconnects unless we're closed/done.
                except asyncio.CancelledError:
                    raise
                except (ConnectionClosed, WebSocketException, OSError, asyncio.TimeoutError, ValueError) as e:
                    self._mark_connection_lost(str(e))
                    logger.warning("[RVCStreamingConverter] WS connection lost/failed: %s", e)

                if self._closed:
                    break
                if self._input_exhausted and self._buffered_bytes == 0:
                    # Nothing left to send and the source is done — no point
                    # reconnecting just to sit idle.
                    break

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX_S)
        finally:
            self._is_healthy = False
            if self._out_queue is not None:
                if self._fatal_error is None:
                    self._put_control(_CLOSE_SENTINEL)

    async def _writer_loop(self, ws):
        """Drains the shared buffer to the currently-connected socket. Stops
        (without closing the socket) once the input is exhausted and the
        buffer is empty; a fresh writer is spun up per reconnect. If a send
        fails (socket died mid-write), the frame is pushed back onto the
        front of the buffer so the next connection's writer resumes it —
        never silently lost."""
        while True:
            buffered = None
            async with self._buffer_lock:
                if self._buffer:
                    buffered = self._buffer.popleft()
                    self._buffered_bytes -= len(buffered.payload)
                    if not self._buffer:
                        self._buffer_not_empty.clear()
            if buffered is None:
                if self._input_exhausted:
                    return
                await self._buffer_not_empty.wait()
                continue
            if time.monotonic() - buffered.enqueued_at > LIVE_EDGE_MAX_AGE_S:
                self.drop_count += 1
                self.stale_input_drop_count += 1
                logger.warning(
                    "[RVCStreamingConverter] dropped stale input frame at send "
                    "(older than 500ms live edge)"
                )
                continue
            try:
                await ws.send(buffered.payload)
                sent_at = time.monotonic()
                if self._sent_bytes_count == 0:
                    self._block_started_at = sent_at
                self._sent_bytes_count += len(buffered.payload)
                while self._sent_bytes_count >= self._block_size_bytes:
                    self._block_sent_timestamps.append(
                        self._block_started_at or sent_at
                    )
                    self._sent_bytes_count -= self._block_size_bytes
                    self._block_started_at = (
                        sent_at if self._sent_bytes_count else None
                    )
            except asyncio.CancelledError:
                await self._requeue_input(buffered)
                raise
            except Exception as e:
                # Send failed on a still-"open" socket (mirror the reader-side
                # log): requeue the un-acked frame and let the connection loop
                # reconnect. Log so a flapping socket is diagnosable.
                logger.warning("[RVCStreamingConverter] WS send failed — requeuing frame, will reconnect: %r", e)
                await self._requeue_input(buffered)
                return

    async def _handle_incoming(self, ws, message):
        if isinstance(message, (bytes, bytearray)):
            # The only bytes this converter ever yields: converted 48kHz PCM
            # received straight from the server.
            await self._enqueue_output(bytes(message))
            return

        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = data.get("type")
        if msg_type == "stats":
            self._apply_stats_sequence(data)
            self.model_version = str(
                data.get("model_version") or self.model_version
            )
            self.profile = str(data.get("profile") or self.profile)
            data["model_version"] = self.model_version
            data["profile"] = self.profile
            data.setdefault("block_ms", self.block_ms)
            data.setdefault("context_ms", self.context_ms)
            data.setdefault("sola_ms", self.sola_ms)
            data.setdefault("sample_rate_in", self.sample_rate_in)
            data.setdefault("sample_rate_out", self.sample_rate_out)
            data.setdefault("use_trt", self.use_trt)
            locked = data.get("locked_pitch")
            if locked is not None and self.adaptive_pitch:
                # Adopt the server's per-call locked shift so any WS reconnect
                # RESUMES this identity (concrete pitch + adaptive_pitch=False in
                # the next _config_payload) instead of re-detecting mid-call.
                try:
                    locked_val = float(locked)
                except (TypeError, ValueError):
                    logger.warning(
                        "[RVCStreamingConverter] ignoring malformed locked_pitch: %r", locked,
                    )
                    locked_val = None
                if locked_val is not None:
                    self.pitch_shift = locked_val
                    self.adaptive_pitch = False
                    logger.info(
                        "[RVCStreamingConverter] adaptive pitch locked at %+.2f st — "
                        "reconnects will resume this value", self.pitch_shift,
                    )
            if self._block_sent_timestamps:
                send_time = self._block_sent_timestamps.popleft()
                rtt_and_wait = (time.monotonic() - send_time) * 1000.0
                data["converter_wait_ms"] = rtt_and_wait
                infer_ms = data.get("total_ms") or data.get("infer_ms") or 0.0
                block_ms = data.get("block_ms") or self.block_ms
                # This is an estimate: first-frame-to-stats includes block
                # accumulation, inference, and network travel. Remove the
                # known server-side portions so they are not mislabeled RTT.
                data["network_rtt_ms"] = max(
                    0.0,
                    rtt_and_wait - float(block_ms) - float(infer_ms),
                )
            if self.on_stats is not None:
                try:
                    self.on_stats({k: v for k, v in data.items() if k != "type"})
                except Exception:
                    logger.exception("[RVCStreamingConverter] on_stats callback raised")
        elif msg_type == "error":
            # Inference failed server-side: it already emitted no audio for
            # that block. Never synthesize/forward raw audio to compensate.
            logger.warning("[RVCStreamingConverter] server error: %s", data.get("message"))
            if data.get("fatal"):
                await self._signal_fatal(str(data.get("message") or "server error"))
                if ws is not None:
                    await ws.close(code=1011, reason="fatal converter error")
        elif msg_type == "ping":
            try:
                await ws.send(json.dumps({"type": "pong"}))
            except Exception:
                pass
        # "pong"/"ready"/"busy" outside the handshake: nothing to do.
