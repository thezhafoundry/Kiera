"""Protocol primitives for the desktop voice-changer audio transport."""

from __future__ import annotations

import hashlib
import asyncio
import contextlib
import math
import secrets
import threading
import time
from collections.abc import Callable
from typing import Literal

from fastapi import WebSocket, WebSocketDisconnect

from .converters.base import VoiceConverter


INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 48000
INPUT_FRAME_BYTES = 640
OUTPUT_FRAME_BYTES = 960
READINESS_TIMEOUT_SECONDS = 150.0

# 48kHz mono 16-bit: how many bytes represent one second of playout audio.
OUTPUT_BYTES_PER_SECOND = OUTPUT_SAMPLE_RATE * 2
# Standing cushion held before the first frame is written, absorbing the
# converter's bursty arrival timing. Mirrors backend/pipeline.py's
# _PLAYOUT_BUFFER_TARGET_BYTES (0.25s) for the LiveKit path.
PLAYOUT_CUSHION_BYTES = int(OUTPUT_BYTES_PER_SECOND * 0.25)
# Hard cap on held backlog; oldest audio is dropped beyond this so a persistent
# stall grows delay only up to a bound.
PLAYOUT_MAX_BYTES = int(OUTPUT_BYTES_PER_SECOND * 5)
# Bytes written per pacing step (100ms) once the cushion has filled.
PLAYOUT_DRAIN_BYTES = int(OUTPUT_BYTES_PER_SECOND * 0.1)

Profile = Literal["male", "female"]


class DesktopSessionStore:
    """Issues single-use, short-lived profile selection tickets."""

    def __init__(
        self,
        ttl_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._tickets: dict[str, tuple[Profile, float]] = {}
        self._lock = threading.Lock()

    def issue(self, profile: Profile) -> tuple[str, int]:
        if profile not in ("male", "female"):
            raise ValueError("profile must be 'male' or 'female'")

        ticket = secrets.token_urlsafe(32)
        ticket_hash = self._hash_ticket(ticket)

        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            expires_at = now + self._ttl_seconds
            self._tickets[ticket_hash] = (profile, expires_at)

        return ticket, self._ttl_seconds

    def consume(self, ticket: str) -> str | None:
        ticket_hash = self._hash_ticket(ticket)

        with self._lock:
            now = self._clock()
            self._purge_expired(now)
            entry = self._tickets.pop(ticket_hash, None)
            if entry is None:
                return None

            profile, expires_at = entry
            if now >= expires_at:
                return None

            return profile

    def _purge_expired(self, now: float) -> None:
        expired_hashes = [
            ticket_hash
            for ticket_hash, (_, expires_at) in self._tickets.items()
            if now >= expires_at
        ]
        for ticket_hash in expired_hashes:
            del self._tickets[ticket_hash]

    @staticmethod
    def _hash_ticket(ticket: str) -> str:
        return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def validate_input_frame(frame: bytes) -> None:
    """Validate a 20 ms 16 kHz mono PCM input frame."""
    if len(frame) != INPUT_FRAME_BYTES:
        raise ValueError(
            f"input frame must be {INPUT_FRAME_BYTES} bytes, got {len(frame)}"
        )


def split_output_frames(buffer: bytearray, chunk: bytes) -> list[bytes]:
    """Append output audio and remove every complete playout frame."""
    buffer.extend(chunk)
    frame_count = len(buffer) // OUTPUT_FRAME_BYTES
    emitted = [
        bytes(buffer[index : index + OUTPUT_FRAME_BYTES])
        for index in range(0, frame_count * OUTPUT_FRAME_BYTES, OUTPUT_FRAME_BYTES)
    ]
    del buffer[: frame_count * OUTPUT_FRAME_BYTES]
    return emitted


def silence_frame() -> bytes:
    """Return one 10 ms silent 48 kHz mono PCM playout frame."""
    return bytes(OUTPUT_FRAME_BYTES)


class DesktopAudioBridge:
    """Fail-closed binary WebSocket relay for one desktop conversion session."""

    def __init__(
        self,
        converter: VoiceConverter,
        input_queue_frames: int = 25,
        readiness_timeout: float = READINESS_TIMEOUT_SECONDS,
        playout_cushion_bytes: int = PLAYOUT_CUSHION_BYTES,
        playout_max_bytes: int = PLAYOUT_MAX_BYTES,
    ) -> None:
        if input_queue_frames < 1:
            raise ValueError("input_queue_frames must be positive")
        if readiness_timeout <= 0:
            raise ValueError("readiness_timeout must be positive")
        if playout_cushion_bytes < 0:
            raise ValueError("playout_cushion_bytes must not be negative")
        if playout_max_bytes < playout_cushion_bytes:
            raise ValueError("playout_max_bytes must be >= playout_cushion_bytes")
        self.converter = converter
        self.input_queue_frames = input_queue_frames
        self.readiness_timeout = readiness_timeout
        self.playout_cushion_bytes = playout_cushion_bytes
        self.playout_max_bytes = playout_max_bytes
        self.input_drop_count = 0
        self.playout_drop_count = 0

    async def run(self, websocket: WebSocket) -> None:
        """Relay fixed-size PCM frames until either side ends the session."""
        try:
            config = await websocket.receive_json()
        except WebSocketDisconnect:
            return
        except Exception as exc:
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "invalid_config",
                    "message": str(exc) or "expected JSON config",
                }
            )
            await websocket.close(code=1008, reason="Invalid audio config")
            return

        if not isinstance(config, dict):
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "invalid_config",
                    "message": "expected JSON config object",
                }
            )
            await websocket.close(code=1008, reason="Invalid audio config")
            return

        expected_config = {
            "type": "config",
            "sample_rate_in": INPUT_SAMPLE_RATE,
            "sample_rate_out": OUTPUT_SAMPLE_RATE,
            "frame_ms": 20,
        }
        for key, expected in expected_config.items():
            if type(config.get(key)) is not type(expected) or config.get(key) != expected:
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "invalid_config",
                        "message": f"{key} must be {expected!r}",
                    }
                )
                await websocket.close(code=1008, reason="Invalid audio config")
                return

        input_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=self.input_queue_frames
        )
        input_closed = asyncio.Event()
        input_enabled = asyncio.Event()
        output_buffer = bytearray()
        playout_enabled = asyncio.Event()
        failed = False
        stats_tasks: set[asyncio.Task[None]] = set()
        send_lock = asyncio.Lock()
        playout_buffer = bytearray()
        playout_lock = asyncio.Lock()
        playout_ready = asyncio.Event()

        def sanitize_stats(data: object) -> dict[str, bool | float | int | str]:
            if not isinstance(data, dict):
                return {}
            sanitized: dict[str, bool | float | int | str] = {}
            for key, value in data.items():
                if not isinstance(key, str) or key == "type" or len(key) > 64:
                    continue
                if isinstance(value, bool):
                    sanitized[key] = value
                elif isinstance(value, int):
                    sanitized[key] = value
                elif isinstance(value, float) and math.isfinite(value):
                    sanitized[key] = value
                elif isinstance(value, str) and len(value) <= 256:
                    sanitized[key] = value
            return sanitized

        async def send_stats(data: dict[str, bool | float | int | str]) -> None:
            try:
                async with send_lock:
                    await websocket.send_json({"type": "stats", **data})
            except (WebSocketDisconnect, RuntimeError):
                return

        def relay_stats(data: object) -> None:
            sanitized = sanitize_stats(data)
            if not sanitized:
                return
            task = asyncio.create_task(send_stats(sanitized))
            stats_tasks.add(task)
            task.add_done_callback(stats_tasks.discard)

        async def drain_stats() -> None:
            if stats_tasks:
                await asyncio.gather(*tuple(stats_tasks), return_exceptions=True)

        if hasattr(self.converter, "on_stats"):
            self.converter.on_stats = relay_stats

        async def input_frames():
            while True:
                if input_closed.is_set() and input_queue.empty():
                    return
                get_input = asyncio.create_task(input_queue.get())
                wait_for_close = asyncio.create_task(input_closed.wait())
                done, pending = await asyncio.wait(
                    {get_input, wait_for_close},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                if get_input in done:
                    yield get_input.result()

        async def receive_input() -> None:
            try:
                while True:
                    frame = await websocket.receive_bytes()
                    try:
                        validate_input_frame(frame)
                    except ValueError as exc:
                        async with send_lock:
                            await websocket.send_json({"type": "error", "message": str(exc)})
                        continue

                    if not input_enabled.is_set():
                        continue

                    if input_queue.full():
                        input_queue.get_nowait()
                        self.input_drop_count += 1
                        async with send_lock:
                            await websocket.send_json(
                                {
                                    "type": "stats",
                                    "input_drop_count": self.input_drop_count,
                                }
                            )
                    input_queue.put_nowait(frame)
            except (WebSocketDisconnect, asyncio.CancelledError):
                return
            finally:
                input_closed.set()

        async def write_frames(payload: bytes) -> None:
            """Write whole playout frames to the client, serialized."""
            for frame in split_output_frames(output_buffer, payload):
                async with send_lock:
                    await websocket.send_bytes(frame)

        async def run_playout_consumer() -> None:
            """Drain the playout buffer at a strictly real-time pace.

            The converter's arrival timing is bursty: measured live on
            2026-07-27, Modal delivered 0.22s of audio, stalled ~8s, then
            dumped 6.2s inside one second. Forwarded unpaced that is audible
            as a fraction of a second of speech then silence. Backlog must
            surface as growing (bounded) delay, never as speed.

            next_publish_time is self-correcting: if a chunk becomes available
            after its deadline already passed (the buffer genuinely ran dry),
            publish immediately and re-anchor from now rather than sleeping to
            "catch up" -- a stale schedule would push the next audio out faster
            than real time, which is the bug this pacer exists to prevent.
            Mirrors backend/pipeline.py::_run_playout_consumer.
            """
            filled = False
            next_publish_time: Optional[float] = None
            try:
                while True:
                    # Checked per-iteration rather than once up front: the
                    # session can end before the readiness gate ever opens
                    # playout, and a single wait() here would strand the
                    # consumer (and the audio it holds) forever.
                    await playout_enabled.wait()
                    async with playout_lock:
                        if not filled:
                            if len(playout_buffer) < self.playout_cushion_bytes:
                                chunk = b""
                            else:
                                chunk = bytes(playout_buffer[: self.playout_cushion_bytes])
                                del playout_buffer[: self.playout_cushion_bytes]
                                filled = True
                        else:
                            chunk = bytes(playout_buffer[:PLAYOUT_DRAIN_BYTES])
                            del playout_buffer[:PLAYOUT_DRAIN_BYTES]
                        # Clear whenever this iteration took no chunk -- that is
                        # exactly when we're about to await playout_ready below,
                        # so the event must not still be set from a stale
                        # append or we spin forever without truly waiting
                        # (reproduced 2026-07-29: buffer sits at a few hundred
                        # ms below the cushion, never empty, so a "clear only
                        # when empty" guard never fires and playout_ready.wait()
                        # returns instantly every iteration -- 100% CPU, zero
                        # audio ever sent). Clearing here is still safe against
                        # the original bug this guarded (swallowing a producer's
                        # set() from the same append that just filled `chunk`):
                        # when a chunk WAS taken we skip this branch entirely,
                        # so a fresh set() from a concurrent append is never
                        # destroyed by it.
                        if not chunk:
                            playout_ready.clear()
                    if chunk:
                        now = time.monotonic()
                        if next_publish_time is None or now >= next_publish_time:
                            next_publish_time = now
                        else:
                            try:
                                await asyncio.sleep(next_publish_time - now)
                            except asyncio.CancelledError:
                                # `chunk` is already popped -- it exists only in
                                # this local now. Honoring the cancellation
                                # without writing it would drop it entirely:
                                # not in the buffer for teardown to flush, not
                                # sent either. Write it before re-raising.
                                await write_frames(chunk)
                                raise
                        await write_frames(chunk)
                        next_publish_time += len(chunk) / OUTPUT_BYTES_PER_SECOND
                    else:
                        await playout_ready.wait()
            except asyncio.CancelledError:
                pass

        async def convert_output() -> None:
            nonlocal failed
            consumer_task: asyncio.Task[None] | None = None
            stream_ended_naturally = False
            try:
                # The consumer must not start writing before the readiness gate
                # opens playout, but the converter stream itself has to start
                # now -- wait_stream_ready() depends on it having begun.
                consumer_task = asyncio.create_task(run_playout_consumer())
                async with contextlib.aclosing(self.converter.convert_stream(input_frames())) as stream:
                    async for chunk in stream:
                        async with playout_lock:
                            playout_buffer.extend(chunk)
                            overflow = len(playout_buffer) - self.playout_max_bytes
                            if overflow > 0:
                                del playout_buffer[:overflow]
                                self.playout_drop_count += overflow
                            playout_ready.set()
                stream_ended_naturally = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                if not playout_enabled.is_set():
                    return
                await drain_stats()
                async with send_lock:
                    await websocket.send_json(
                        {"type": "error", "message": f"conversion failed: {exc}"}
                    )
                    await websocket.send_bytes(silence_frame())
                    await websocket.close(code=1011, reason="Conversion failed")
            finally:
                if consumer_task is not None:
                    consumer_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await consumer_task
                # The consumer paces to real time and can be mid-sleep holding
                # backlog when the converter stream ends. Flush the remainder
                # immediately/unpaced rather than truncating the tail -- a brief
                # blip on the last fragment beats losing it outright. Only on a
                # natural end: if we were cancelled or the conversion failed,
                # the session is going away and nobody is listening.
                if stream_ended_naturally and not failed:
                    async with playout_lock:
                        leftover = bytes(playout_buffer)
                        playout_buffer.clear()
                    if leftover:
                        with contextlib.suppress(Exception):
                            await write_frames(leftover)

        wait_stream_ready = getattr(self.converter, "wait_stream_ready", None)
        needs_stream_readiness = callable(wait_stream_ready)
        readiness_task: asyncio.Task[bool] | None = None
        receive_task: asyncio.Task[None] | None = None
        convert_task: asyncio.Task[None] | None = None
        converter_closed = False

        async def cleanup_tasks() -> None:
            nonlocal converter_closed
            if not converter_closed:
                with contextlib.suppress(Exception):
                    await self.aclose()
                converter_closed = True
            for task in (readiness_task, receive_task, convert_task):
                if task is not None and not task.done():
                    task.cancel()
            for task in (readiness_task, receive_task, convert_task):
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task

        async def await_stream_ready() -> bool:
            try:
                async with asyncio.timeout(self.readiness_timeout):
                    converter_ready = await wait_stream_ready(self.readiness_timeout)
            except asyncio.CancelledError:
                raise
            except Exception:
                converter_ready = False
            return converter_ready is True

        try:
            if not needs_stream_readiness:
                input_enabled.set()
            receive_task = asyncio.create_task(receive_input())
            convert_task = asyncio.create_task(convert_output())
            # Let the async generator enter its long-lived stream setup before
            # waiting for an optional persistent-session readiness hook.
            await asyncio.sleep(0)

            if needs_stream_readiness:
                readiness_task = asyncio.create_task(await_stream_ready())
                done, _ = await asyncio.wait(
                    {readiness_task, receive_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task in done:
                    await cleanup_tasks()
                    with contextlib.suppress(Exception):
                        await websocket.close(code=1000, reason="Client disconnected")
                    return
                converter_ready = readiness_task.result()

                if not converter_ready:
                    await cleanup_tasks()
                    async with send_lock:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "code": "converter_unavailable",
                                "message": "conversion backend unavailable",
                            }
                        )
                        await websocket.close(code=1011, reason="Conversion backend unavailable")
                    return

            if needs_stream_readiness and receive_task.done():
                await cleanup_tasks()
                with contextlib.suppress(Exception):
                    await websocket.close(code=1000, reason="Client disconnected")
                return

            async with send_lock:
                await websocket.send_json({"type": "ready"})
            input_enabled.set()
            playout_enabled.set()
            done, pending = await asyncio.wait(
                {receive_task, convert_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if receive_task in done and not convert_task.done():
                if needs_stream_readiness:
                    await cleanup_tasks()
                else:
                    try:
                        converter_closed = await self.aclose()
                    except Exception:
                        converter_closed = False
                    if converter_closed:
                        with contextlib.suppress(asyncio.CancelledError):
                            await convert_task
                    else:
                        # Generic converters are expected to finish after their
                        # input iterator closes; preserve accepted buffered frames.
                        await convert_task
            elif convert_task in done and not receive_task.done():
                await cleanup_tasks()

            for task in pending:
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            await drain_stats()
            if not failed:
                async with send_lock:
                    await websocket.send_json(
                        {"type": "stopped", "input_drop_count": self.input_drop_count}
                    )
                    await websocket.close()
        finally:
            await cleanup_tasks()
            with contextlib.suppress(Exception):
                await websocket.close()

    async def aclose(self) -> bool:
        """Release a converter that exposes an explicit async close operation."""
        close = getattr(self.converter, "close", None)
        if close is None:
            return False
        await close()
        return True
