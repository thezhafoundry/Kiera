"""Protocol primitives for the desktop voice-changer audio transport."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from typing import Literal


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
        expires_at = self._clock() + self._ttl_seconds

        with self._lock:
            self._tickets[ticket_hash] = (profile, expires_at)

        return ticket, self._ttl_seconds

    def consume(self, ticket: str) -> str | None:
        ticket_hash = self._hash_ticket(ticket)

        with self._lock:
            entry = self._tickets.pop(ticket_hash, None)

        if entry is None:
            return None

        profile, expires_at = entry
        if self._clock() >= expires_at:
            return None

        return profile

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
