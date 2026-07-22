"""Tests for the desktop audio transport contracts."""

import pytest

from backend.desktop_audio import (
    DesktopSessionStore,
    silence_frame,
    split_output_frames,
    validate_input_frame,
)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_input_frame_contract():
    validate_input_frame(bytes(640))

    with pytest.raises(ValueError):
        validate_input_frame(bytes(639))


def test_output_framing_retains_partial_tail():
    pending = bytearray()

    assert split_output_frames(pending, bytes(961)) == [bytes(960)]
    assert pending == bytearray(bytes(1))


def test_session_ticket_carries_profile_and_is_single_use():
    store = DesktopSessionStore(ttl_seconds=1, clock=lambda: 100.0)
    ticket, expires_in = store.issue("male")

    assert expires_in == 1
    assert store.consume(ticket) == "male"
    assert store.consume(ticket) is None


def test_session_ticket_is_valid_immediately_before_expiry():
    clock = FakeClock()
    store = DesktopSessionStore(ttl_seconds=1, clock=clock)
    ticket, _ = store.issue("female")
    clock.now = 100.999999

    assert store.consume(ticket) == "female"


def test_issuing_ticket_purges_expired_tickets():
    clock = FakeClock()
    store = DesktopSessionStore(ttl_seconds=1, clock=clock)
    expired_ticket, _ = store.issue("male")
    clock.now = 101.0
    store.issue("female")

    assert len(store._tickets) == 1
    assert store.consume(expired_ticket) is None


def test_silence_frame_matches_output_contract():
    assert silence_frame() == bytes(960)
