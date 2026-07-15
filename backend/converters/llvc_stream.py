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
    ):
        self.ws_url = ws_url
        self.api_key = api_key
        self.connect_timeout = connect_timeout
        self.drop_count = 0
        self._block_sent_timestamps = deque()
        self._sent_bytes_count = 0

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

    def _config_payload(self) -> str:
        return json.dumps({
            "type": "config",
            "api_key": self.api_key,
        })

    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        self._buffer = deque()
        self._buffered_bytes = 0
        self._buffer_lock = asyncio.Lock()
        self._buffer_not_empty = asyncio.Event()
        self._input_exhausted = False
        self._closed = False
        self._out_queue = asyncio.Queue()
        self._sent_bytes_count = 0
        self._block_sent_timestamps = deque()

        self._pump_task = asyncio.create_task(self._pump_input(in_audio))
        self._conn_task = asyncio.create_task(self._connection_loop())

        try:
            while True:
                item = await self._out_queue.get()
                if item is _CLOSE_SENTINEL:
                    break
                yield item
        finally:
            await self._teardown()

    async def close(self):
        self._closed = True
        if self._conn_task:
            self._conn_task.cancel()
        if self._pump_task:
            self._pump_task.cancel()

    async def _teardown(self):
        self._closed = True
        # Cancel running background loops
        for task in [self._pump_task, self._conn_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _pump_input(self, in_audio: AsyncIterator[bytes]):
        try:
            async for frame in in_audio:
                if self._closed:
                    break
                async with self._buffer_lock:
                    self._buffer.append(frame)
                    self._buffered_bytes += len(frame)
                    while self._buffered_bytes > _MAX_BUFFER_BYTES and self._buffer:
                        dropped = self._buffer.popleft()
                        self._buffered_bytes -= len(dropped)
                        self.drop_count += 1
                        logger.warning(
                            "[LLVCStreamingConverter] reconnect buffer full (500ms cap) — "
                            "dropped oldest input frame (%d bytes)",
                            len(dropped),
                        )
                self._buffer_not_empty.set()
        except asyncio.CancelledError:
            raise
        finally:
            self._input_exhausted = True
            self._buffer_not_empty.set()

    async def _connection_loop(self):
        backoff = _BACKOFF_INITIAL_S
        try:
            while not self._closed:
                try:
                    connect_kwargs = {
                        "open_timeout": self.connect_timeout,
                        "max_size": None,
                    }
                    if self.api_key:
                        connect_kwargs["additional_headers"] = {"Authorization": f"Bearer {self.api_key}"}

                    async with websockets.connect(
                        self.ws_url,
                        **connect_kwargs,
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
                        logger.info("[LLVCStreamingConverter] connected to LLVC server and handshake succeeded")

                        writer_task = asyncio.create_task(self._writer_loop(ws))
                        reader_task = asyncio.create_task(self._reader_loop(ws))

                        done, pending = await asyncio.wait(
                            [writer_task, reader_task],
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for t in pending:
                            t.cancel()

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning("[LLVCStreamingConverter] WS connection lost/failed: %s", e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, _BACKOFF_MAX_S)
        except asyncio.CancelledError:
            pass
        finally:
            if self._out_queue:
                await self._out_queue.put(_CLOSE_SENTINEL)

    async def _writer_loop(self, ws):
        while True:
            frame = None
            async with self._buffer_lock:
                if self._buffer:
                    frame = self._buffer.popleft()
                    self._buffered_bytes -= len(frame)
                    if not self._buffer:
                        self._buffer_not_empty.clear()
            if frame is None:
                if self._input_exhausted:
                    return
                await self._buffer_not_empty.wait()
                continue
            try:
                await ws.send(frame)
                self._sent_bytes_count += len(frame)
                bytes_per_block = int(16000 * 2 * 0.32)  # 320ms block = 10240 bytes
                if self._sent_bytes_count >= bytes_per_block:
                    self._block_sent_timestamps.append(time.monotonic())
                    self._sent_bytes_count -= bytes_per_block
            except asyncio.CancelledError:
                async with self._buffer_lock:
                    self._buffer.appendleft(frame)
                    self._buffered_bytes += len(frame)
                    self._buffer_not_empty.set()
                raise
            except Exception as e:
                logger.warning("[LLVCStreamingConverter] WS send failed — requeuing frame, will reconnect: %r", e)
                async with self._buffer_lock:
                    self._buffer.appendleft(frame)
                    self._buffered_bytes += len(frame)
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
            await self._out_queue.put(bytes(message))
            return

        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = data.get("type")
        if msg_type == "stats":
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
            logger.warning("[LLVCStreamingConverter] server error: %s", data.get("message"))
        elif msg_type == "ping":
            try:
                await ws.send(json.dumps({"type": "pong"}))
            except Exception:
                pass

    async def wait_ready(self, timeout: float) -> bool:
        try:
            async with asyncio.timeout(timeout):
                connect_kwargs = {
                    "open_timeout": timeout,
                    "max_size": None,
                }
                if self.api_key:
                    connect_kwargs["additional_headers"] = {"Authorization": f"Bearer {self.api_key}"}

                async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                    await ws.send(self._config_payload())
                    while True:
                        reply = await ws.recv()
                        if isinstance(reply, (bytes, bytearray)):
                            continue
                        data = json.loads(reply)
                        msg_type = data.get("type")
                        if msg_type == "ready":
                            return True
                        if msg_type in ("busy", "error"):
                            return False
        except Exception as e:
            logger.warning("[LLVCStreamingConverter] wait_ready failed: %s", e)
            return False
