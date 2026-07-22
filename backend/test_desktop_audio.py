"""Tests for the desktop audio transport contracts."""

import pytest

from backend.desktop_audio import (
    DesktopSessionStore,
    silence_frame,
    split_output_frames,
    validate_input_frame,
)


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


def test_silence_frame_matches_output_contract():
    assert silence_frame() == bytes(960)
