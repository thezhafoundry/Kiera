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
# Cap at 500ms of buffered *input* audio for LLVC reconnection.
_MAX_BUFFER_BYTES = int(_INPUT_BYTES_PER_SECOND * 0.5)

_BACKOFF_INITIAL_S = 0.5
_BACKOFF_MAX_S = 5.0

_CLOSE_SENTINEL = object()


class LLVCStreamingConverter(VoiceConverter):
    """
    LLVC streaming converter that manages a persistent WebSocket connection to a
    remote LLVC GPU/CPU conversion server.
    """

    def __init__(
        self,
        ws_url: str,
        api_key: Optional[str] = None,
        connect_timeout: float = 5.0,
        output_queue_max_chunks: int = DEFAULT_OUTPUT_QUEUE_MAX_CHUNKS,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        heartbeat_timeout: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
        model_version: str = "unknown",
    ):
        self.ws_url = ws_url.rstrip("/")
        self.api_key = api_key or ""
        validate_streaming_endpoint(self.ws_url, self.api_key)
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
        self._block_sent_timestamps = deque()
        self._sent_bytes_count = 0
        self._stats_sequence = 0

        # Optional callback: on_stats(dict) — called with server's stats payload
        self.on_stats: Optional[Callable[[dict], None]] = None

        # Duplex state
        self._buffer: deque = deque()
        self._buffered_bytes = 0
        self._buffer_lock: Optional[asyncio.Lock] = None
        self._buffer_not_empty: Optional[asyncio.Event] = None
        self._input_exhausted = False
        self._closed = False
        self._out_queue: Optional[asyncio.Queue] = None
        self._pump_task: Optional[asyncio.Task] = None
        self._conn_task: Optional[asyncio.Task] = None
        self._session_ready = asyncio.Event()
        self._is_healthy = False
        self._fatal_error: Optional[RuntimeError] = None

    @property
    def is_healthy(self) -> bool:
        """True only while the call's actual conversion socket is handshaken."""
        return self._is_healthy and not self._closed

    def _mark_connection_lost(self, reason: str) -> None:
        was_healthy = self._is_healthy
        self._is_healthy = False
        if was_healthy:
            self.connection_failure_count += 1
            logger.warning(
                "[LLVCStreamingConverter] active session became unhealthy: %s",
                reason,
            )

    def _config_payload(self) -> str:
        return json.dumps({"type": "config"})

    def _connect_kwargs(self) -> dict:
        kwargs = {
            "open_timeout": self.connect_timeout,
            "max_size": self.max_message_size,
            "ping_interval": self.heartbeat_interval,
            "ping_timeout": self.heartbeat_timeout,
        }
        if self.api_key:
            kwargs["additional_headers"] = {
                "Authorization": f"Bearer {self.api_key}"
            }
        return kwargs

    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        self._buffer = deque()
        self._buffered_bytes = 0
        self._buffer_lock = asyncio.Lock()
        self._buffer_not_empty = asyncio.Event()
        self._input_exhausted = False
        self._closed = False
        self._out_queue = asyncio.Queue(maxsize=self.output_queue_max_chunks)
        self._sent_bytes_count = 0
        self._block_sent_timestamps = deque()
        self._stats_sequence = 0
        self.drop_count = 0
        self.stale_input_drop_count = 0
        self.input_overflow_drop_count = 0
        self.output_drop_count = 0
        self.connection_failure_count = 0
        self._fatal_error = None
        self._session_ready.clear()
        self._is_healthy = False

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

    async def close(self):
        self._closed = True
        self._is_healthy = False
        self._session_ready.clear()
        if self._out_queue is not None:
            self._put_control(_CLOSE_SENTINEL)
        await self._teardown()

    async def _teardown(self):
        self._closed = True
        self._is_healthy = False
        self._session_ready.clear()
        # Cancel running background loops
        for task in (self._pump_task, self._conn_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._pump_task, self._conn_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception(
                        "[LLVCStreamingConverter] background task failed during teardown"
                    )

    async def _next_output(self):
        item = await self._out_queue.get()
        if isinstance(item, FatalOutput):
            raise item.error
        return item

    def _put_control(self, item) -> None:
        if self._out_queue is None:
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
                "[LLVCStreamingConverter] converted output queue full — "
                "dropped oldest chunk"
            )
        self._out_queue.put_nowait(chunk)

    async def _signal_fatal(self, message: str) -> None:
        self._is_healthy = False
        self._session_ready.clear()
        self._closed = True
        self._fatal_error = RuntimeError(
            f"LLVC streaming converter fatal error: {message}"
        )
        self._put_control(FatalOutput(self._fatal_error))

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
                    "[LLVCStreamingConverter] reconnect buffer full (500ms cap) — "
                    "dropped oldest input frame (%d bytes)",
                    len(dropped.payload),
                )
        self._buffer_not_empty.set()

    async def _connection_loop(self):
        backoff = _BACKOFF_INITIAL_S
        try:
            while not self._closed:
                try:
                    async with websockets.connect(
                        self.ws_url,
                        **self._connect_kwargs(),
                    ) as ws:
                        # 1. Config Handshake
                        await ws.send(self._config_payload())
                        try:
                            handshake = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            logger.warning("[LLVCStreamingConverter] handshake timeout, reconnecting")
                            continue

                        try:
                            hs_data = json.loads(handshake)
                        except (json.JSONDecodeError, TypeError):
                            logger.warning("[LLVCStreamingConverter] malformed handshake config, reconnecting")
                            continue

                        hs_type = hs_data.get("type")
                        if hs_type == "busy":
                            logger.warning(
                                "[LLVCStreamingConverter] server busy: %s — waiting before retry",
                                hs_data.get("reason", "unknown"),
                            )
                            await asyncio.sleep(5.0)
                            continue
                        elif hs_type != "ready":
                            logger.warning("[LLVCStreamingConverter] unexpected handshake response: %s", hs_type)
                            continue

                        # Reset backoff on successful connection
                        backoff = _BACKOFF_INITIAL_S
                        self._is_healthy = True
                        self.model_version = str(
                            hs_data.get("model_version") or self.model_version
                        )
                        self._session_ready.set()
                        logger.info("[LLVCStreamingConverter] connected to LLVC server and handshake succeeded")

                        writer_task = asyncio.create_task(self._writer_loop(ws))
                        reader_task = asyncio.create_task(self._reader_loop(ws))
                        try:
                            done, pending = await asyncio.wait(
                                [writer_task, reader_task],
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for t in pending:
                                t.cancel()
                        finally:
                            self._mark_connection_lost("reader or writer stopped")
                            self._session_ready.clear()

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._mark_connection_lost(str(e))
                    self._session_ready.clear()
                    logger.warning("[LLVCStreamingConverter] WS connection lost/failed: %s", e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, _BACKOFF_MAX_S)
        except asyncio.CancelledError:
            pass
        finally:
            self._is_healthy = False
            self._session_ready.clear()
            if self._out_queue:
                if self._fatal_error is None:
                    self._put_control(_CLOSE_SENTINEL)

    async def _writer_loop(self, ws):
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
                    "[LLVCStreamingConverter] dropped stale input frame at send "
                    "(older than 500ms live edge)"
                )
                continue
            try:
                await ws.send(buffered.payload)
                self._sent_bytes_count += len(buffered.payload)
                bytes_per_block = int(16000 * 2 * 0.32)  # 320ms block = 10240 bytes
                if self._sent_bytes_count >= bytes_per_block:
                    self._block_sent_timestamps.append(time.monotonic())
                    self._sent_bytes_count -= bytes_per_block
            except asyncio.CancelledError:
                async with self._buffer_lock:
                    self._buffer.appendleft(buffered)
                    self._buffered_bytes += len(buffered.payload)
                    self._buffer_not_empty.set()
                raise
            except Exception as e:
                logger.warning("[LLVCStreamingConverter] WS send failed — requeuing frame, will reconnect: %r", e)
                async with self._buffer_lock:
                    self._buffer.appendleft(buffered)
                    self._buffered_bytes += len(buffered.payload)
                    self._buffer_not_empty.set()
                return

    async def _reader_loop(self, ws):
        try:
            async for message in ws:
                await self._handle_incoming(ws, message)
        except ConnectionClosed:
            pass

    async def _handle_incoming(self, ws, message):
        if isinstance(message, (bytes, bytearray)):
            await self._enqueue_output(bytes(message))
            return

        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = data.get("type")
        if msg_type == "stats":
            self._stats_sequence += 1
            data.setdefault("sequence_id", self._stats_sequence)
            if data.get("model_version"):
                self.model_version = str(data["model_version"])
            data.setdefault("model_version", self.model_version)
            if self._block_sent_timestamps:
                send_time = self._block_sent_timestamps.popleft()
                rtt_and_wait = (time.monotonic() - send_time) * 1000.0
                data["converter_wait_ms"] = rtt_and_wait
                infer_ms = data.get("total_ms") or data.get("infer_ms") or 0.0
                data["network_rtt_ms"] = max(0.0, rtt_and_wait - infer_ms)
            if self.on_stats is not None:
                try:
                    self.on_stats({k: v for k, v in data.items() if k != "type"})
                except Exception:
                    logger.exception("[LLVCStreamingConverter] on_stats callback raised")
        elif msg_type == "error":
            self._is_healthy = False
            self._session_ready.clear()
            logger.warning("[LLVCStreamingConverter] server error: %s", data.get("message"))
            if data.get("fatal"):
                await self._signal_fatal(str(data.get("message") or "server error"))
                if ws is not None:
                    await ws.close(code=1011, reason="fatal converter error")
        elif msg_type == "ping":
            try:
                await ws.send(json.dumps({"type": "pong"}))
            except Exception:
                pass

    async def wait_ready(self, timeout: float) -> bool:
        """Wait for the call's real conversion session to finish its handshake.

        A standalone probe would be a different socket and creates a TOCTOU race:
        it can succeed even if the session that will carry call audio is rejected.
        """
        if self._conn_task is None or self._conn_task.done() or self._closed:
            return False
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=timeout)
            return self.is_healthy
        except Exception as e:
            logger.warning("[LLVCStreamingConverter] wait_ready failed: %s", e)
            return False
