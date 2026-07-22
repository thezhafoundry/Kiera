"""Protocol primitives for the desktop voice-changer audio transport."""

from __future__ import annotations

import hashlib
import asyncio
import contextlib
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
    ) -> None:
        if input_queue_frames < 1:
            raise ValueError("input_queue_frames must be positive")
        self.converter = converter
        self.input_queue_frames = input_queue_frames
        self.input_drop_count = 0

    async def run(self, websocket: WebSocket) -> None:
        """Relay fixed-size PCM frames until either side ends the session."""
        input_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=self.input_queue_frames
        )
        input_closed = asyncio.Event()
        output_buffer = bytearray()
        failed = False

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
                        await websocket.send_json({"type": "error", "message": str(exc)})
                        continue

                    if input_queue.full():
                        input_queue.get_nowait()
                        self.input_drop_count += 1
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

        async def convert_output() -> None:
            nonlocal failed
            try:
                async with contextlib.aclosing(self.converter.convert_stream(input_frames())) as stream:
                    async for chunk in stream:
                        for frame in split_output_frames(output_buffer, chunk):
                            await websocket.send_bytes(frame)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                await websocket.send_json(
                    {"type": "error", "message": f"conversion failed: {exc}"}
                )
                await websocket.send_bytes(silence_frame())
                await websocket.close(code=1011, reason="Conversion failed")

        await websocket.send_json({"type": "ready"})
        receive_task = asyncio.create_task(receive_input())
        convert_task = asyncio.create_task(convert_output())
        done, pending = await asyncio.wait(
            {receive_task, convert_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if receive_task in done and not convert_task.done():
            await convert_task
        elif convert_task in done and not receive_task.done():
            receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receive_task

        for task in pending:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if not failed:
            await websocket.send_json(
                {"type": "stopped", "input_drop_count": self.input_drop_count}
            )
            await websocket.close()

    async def aclose(self) -> None:
        """Release a converter that exposes an explicit async close operation."""
        close = getattr(self.converter, "close", None)
        if close is not None:
            await close()
