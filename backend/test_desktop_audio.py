"""Tests for the desktop audio transport contracts and relay."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from backend.desktop_audio import (
    DesktopAudioBridge,
    DesktopSessionStore,
    silence_frame,
    split_output_frames,
    validate_input_frame,
)
from backend.converters.base import VoiceConverter


class FakeWebSocket:
    """In-memory binary WebSocket used to exercise relay behavior."""

    def __init__(self, incoming: list[bytes | BaseException]) -> None:
        self._incoming: asyncio.Queue[bytes | BaseException] = asyncio.Queue()
        for message in incoming:
            self._incoming.put_nowait(message)
        self.binary_messages: list[bytes] = []
        self.json_messages: list[dict] = []
        self.closed = False

    async def receive_bytes(self) -> bytes:
        message = await self._incoming.get()
        if isinstance(message, BaseException):
            raise message
        return message

    async def send_bytes(self, message: bytes) -> None:
        self.binary_messages.append(message)

    async def send_json(self, message: dict) -> None:
        self.json_messages.append(message)

    async def close(self, **_kwargs) -> None:
        self.closed = True


class FakeConverter(VoiceConverter):
    def __init__(
        self,
        output: bytes = b"",
        *,
        fail: bool = False,
        start: asyncio.Event | None = None,
    ) -> None:
        self.output = output
        self.fail = fail
        self.start = start
        self.inputs: list[bytes] = []

    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        if self.start is not None:
            await self.start.wait()
        async for frame in in_audio:
            self.inputs.append(frame)
            if self.fail:
                raise RuntimeError("fake conversion failed")
            if self.output:
                yield self.output


async def run_bridge(websocket: FakeWebSocket, converter: FakeConverter, **kwargs) -> None:
    bridge = DesktopAudioBridge(converter, **kwargs)
    await asyncio.wait_for(bridge.run(websocket), timeout=1)


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


@pytest.mark.asyncio
async def test_bridge_sends_ready_and_converted_output_frames():
    sentinel = b"input-sentinel" + bytes(640 - len("input-sentinel"))
    websocket = FakeWebSocket([sentinel, asyncio.CancelledError()])
    converter = FakeConverter(output=bytes(1921))

    await run_bridge(websocket, converter)

    assert websocket.json_messages[0]["type"] == "ready"
    assert converter.inputs == [sentinel]
    assert websocket.binary_messages == [bytes(960), bytes(960)]
    assert all(sentinel not in message for message in websocket.binary_messages)


@pytest.mark.asyncio
async def test_bridge_rejects_malformed_input_without_converter_input():
    websocket = FakeWebSocket([bytes(639), asyncio.CancelledError()])
    converter = FakeConverter()

    await run_bridge(websocket, converter)

    assert converter.inputs == []
    assert any(message["type"] == "error" for message in websocket.json_messages)


@pytest.mark.asyncio
async def test_bridge_drops_oldest_input_when_queue_is_full():
    start = asyncio.Event()
    frames = [bytes([index]) * 640 for index in range(3)]
    websocket = FakeWebSocket([*frames, asyncio.CancelledError()])
    converter = FakeConverter(start=start)
    bridge = DesktopAudioBridge(converter, input_queue_frames=2)

    task = asyncio.create_task(bridge.run(websocket))
    await asyncio.sleep(0)
    start.set()
    await asyncio.wait_for(task, timeout=1)

    assert bridge.input_drop_count == 1
    assert converter.inputs == frames[1:]
    assert any(
        message["type"] == "stats" and message["input_drop_count"] == 1
        for message in websocket.json_messages
    )


@pytest.mark.asyncio
async def test_bridge_fails_closed_when_converter_raises():
    sentinel = b"input-sentinel" + bytes(640 - len("input-sentinel"))
    websocket = FakeWebSocket([sentinel])
    converter = FakeConverter(fail=True)

    await run_bridge(websocket, converter)

    types = [message["type"] for message in websocket.json_messages]
    assert "error" in types
    assert websocket.binary_messages == [silence_frame()]
    assert all(sentinel not in message for message in websocket.binary_messages)
    assert websocket.closed


def test_desktop_session_requires_control_token_and_issues_single_use_ticket(monkeypatch):
    from backend import main

    monkeypatch.setattr(main, "CONTROL_PLANE_TOKEN", "desktop-test-token")
    with TestClient(main.app) as client:
        assert client.post("/api/desktop/session", json={"profile": "male"}).status_code == 401

        response = client.post(
            "/api/desktop/session",
            json={"profile": "female"},
            headers={"Authorization": "Bearer desktop-test-token"},
        )

    assert response.status_code == 200
    ticket = response.json()["ticket"]
    assert response.json()["expires_in"] > 0
    assert main.app.state.desktop_sessions.consume(ticket) == "female"
    assert main.app.state.desktop_sessions.consume(ticket) is None


def test_desktop_audio_websocket_consumes_subprotocol_ticket(monkeypatch):
    from backend import main

    captured: dict[str, object] = {}

    class StubConverter:
        def __init__(self, **kwargs) -> None:
            captured["converter"] = kwargs

    class StubBridge:
        def __init__(self, converter) -> None:
            captured["bridge_converter"] = converter

        async def run(self, websocket) -> None:
            await websocket.send_json({"type": "ready"})

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(main, "RVC_ENDPOINT_URL", "https://example.test/convert")
    monkeypatch.setattr(main, "RVC_API_KEY", "test-api-key")
    monkeypatch.setattr(main, "RVCStreamingConverter", StubConverter)
    monkeypatch.setattr(main, "DesktopAudioBridge", StubBridge)
    with TestClient(main.app) as client:
        ticket, _ = main.app.state.desktop_sessions.issue("male")
        with client.websocket_connect(
            "/api/desktop/audio",
            subprotocols=[f"keira-desktop.{ticket}"],
        ) as websocket:
            assert websocket.receive_json() == {"type": "ready"}

    assert captured["converter"]["pitch_shift"] == main.RVC_MALE_PITCH_SHIFT
