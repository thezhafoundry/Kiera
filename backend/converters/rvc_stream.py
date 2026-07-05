import asyncio
import json
import logging
import os
from collections import deque
from typing import AsyncIterator, Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from .base import VoiceConverter

logger = logging.getLogger(__name__)

# Bytes/second of raw 16kHz mono 16-bit PCM input (client -> server direction).
_INPUT_BYTES_PER_SECOND = 16000 * 2
# Hold-don't-leak reconnect buffer cap: 5s of buffered *input* audio. Beyond this,
# oldest frames are dropped (never raw audio to the lead — this only affects what
# gets converted once the socket reconnects).
_MAX_BUFFER_BYTES = _INPUT_BYTES_PER_SECOND * 5

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
        pitch_shift: int = -1,
        index_rate: float = 0.75,
        rms_mix_rate: float = 0.75,
        protect: float = 0.33,
        f0_method: str = "",
        connect_timeout: float = 10.0,
    ):
        self.ws_url = (
            ws_url
            or os.getenv("RVC_WS_URL", "")
            or derive_ws_url(endpoint_url or os.getenv("RVC_ENDPOINT_URL", ""))
        ).rstrip("/")
        self.api_key = api_key
        self.pitch_shift = pitch_shift
        self.index_rate = index_rate
        self.rms_mix_rate = rms_mix_rate
        self.protect = protect
        self.f0_method = f0_method or os.getenv("RVC_F0_METHOD", "pm")
        self.connect_timeout = connect_timeout

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

    def _config_payload(self) -> str:
        return json.dumps({
            "type": "config",
            "pitch_shift": self.pitch_shift,
            "index_rate": self.index_rate,
            "rms_mix_rate": self.rms_mix_rate,
            "protect": self.protect,
            "f0_method": self.f0_method,
        })

    def _connect_kwargs(self) -> dict:
        kwargs = {"open_timeout": self.connect_timeout, "max_size": None}
        if self.api_key:
            kwargs["additional_headers"] = {"Authorization": f"Bearer {self.api_key}"}
        return kwargs

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
                            return True
                        if msg_type in ("busy", "error"):
                            return False
                        # ignore anything else (e.g. stray "pong") and keep waiting
        except Exception as e:
            logger.warning("[RVCStreamingConverter] wait_ready failed: %s", e)
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
        self._out_queue = asyncio.Queue()

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

    async def _teardown(self):
        self._closed = True
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
        if self._out_queue is not None:
            self._out_queue.put_nowait(_CLOSE_SENTINEL)
        await self._teardown()

    # ---- input pump: continuously drains in_audio into the bounded buffer ----
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
                        logger.warning(
                            "[RVCStreamingConverter] reconnect buffer full (%ds cap) — "
                            "dropped oldest input frame (%d bytes)",
                            _MAX_BUFFER_BYTES // _INPUT_BYTES_PER_SECOND, len(dropped),
                        )
                self._buffer_not_empty.set()
        except asyncio.CancelledError:
            raise
        finally:
            self._input_exhausted = True
            self._buffer_not_empty.set()

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
                        if data.get("type") != "ready":
                            raise WebSocketException(f"unexpected handshake reply: {data}")

                        backoff = _BACKOFF_INITIAL_S  # reset after a successful handshake

                        writer_task = asyncio.create_task(self._writer_loop(ws))
                        try:
                            async for message in ws:
                                await self._handle_incoming(ws, message)
                        finally:
                            writer_task.cancel()
                            try:
                                await writer_task
                            except (asyncio.CancelledError, Exception):
                                pass

                        # Reader loop only ends when the socket closes (or we're
                        # shutting down); either way fall through to the outer
                        # while-loop, which reconnects unless we're closed/done.
                except asyncio.CancelledError:
                    raise
                except (ConnectionClosed, WebSocketException, OSError, asyncio.TimeoutError, ValueError) as e:
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
            if self._out_queue is not None:
                self._out_queue.put_nowait(_CLOSE_SENTINEL)

    async def _writer_loop(self, ws):
        """Drains the shared buffer to the currently-connected socket. Stops
        (without closing the socket) once the input is exhausted and the
        buffer is empty; a fresh writer is spun up per reconnect. If a send
        fails (socket died mid-write), the frame is pushed back onto the
        front of the buffer so the next connection's writer resumes it —
        never silently lost."""
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
            except asyncio.CancelledError:
                async with self._buffer_lock:
                    self._buffer.appendleft(frame)
                    self._buffered_bytes += len(frame)
                    self._buffer_not_empty.set()
                raise
            except Exception:
                # Send failed on a still-"open" socket (mirror the reader-side
                # log): requeue the un-acked frame and let the connection loop
                # reconnect. Log so a flapping socket is diagnosable.
                logger.warning("[RVCStreamingConverter] WS send failed — requeuing frame, will reconnect")
                async with self._buffer_lock:
                    self._buffer.appendleft(frame)
                    self._buffered_bytes += len(frame)
                    self._buffer_not_empty.set()
                return

    async def _handle_incoming(self, ws, message):
        if isinstance(message, (bytes, bytearray)):
            # The only bytes this converter ever yields: converted 48kHz PCM
            # received straight from the server.
            await self._out_queue.put(bytes(message))
            return

        try:
            data = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = data.get("type")
        if msg_type == "stats":
            if self.on_stats is not None:
                try:
                    self.on_stats({k: v for k, v in data.items() if k != "type"})
                except Exception:
                    logger.exception("[RVCStreamingConverter] on_stats callback raised")
        elif msg_type == "error":
            # Inference failed server-side: it already emitted no audio for
            # that block. Never synthesize/forward raw audio to compensate.
            logger.warning("[RVCStreamingConverter] server error: %s", data.get("message"))
        elif msg_type == "ping":
            try:
                await ws.send(json.dumps({"type": "pong"}))
            except Exception:
                pass
        # "pong"/"ready"/"busy" outside the handshake: nothing to do.
