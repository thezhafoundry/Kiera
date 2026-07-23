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

    def __init__(self, incoming: list[bytes | dict | BaseException]) -> None:
        self._incoming: asyncio.Queue[bytes | dict | BaseException] = asyncio.Queue()
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

    async def receive_json(self) -> dict:
        message = await self._incoming.get()
        if isinstance(message, BaseException):
            raise message
        if not isinstance(message, dict):
            raise ValueError("expected JSON config")
        return message

    async def send_bytes(self, message: bytes) -> None:
        self.binary_messages.append(message)

    async def send_json(self, message: dict) -> None:
        self.json_messages.append(message)

    async def close(self, **_kwargs) -> None:
        self.closed = True

    def push(self, message: bytes | dict | BaseException) -> None:
        self._incoming.put_nowait(message)


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


class ReadinessConverter(FakeConverter):
    def __init__(
        self,
        *,
        ready: bool = True,
        failure: BaseException | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        super().__init__()
        self.ready = ready
        self.failure = failure
        self.release = release
        self.probe_started = asyncio.Event()
        self.probe_finished = asyncio.Event()
        self.stream_started = asyncio.Event()
        self.probe_timeout: float | None = None
        self.close_called = False

    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        self.stream_started.set()
        async for chunk in super().convert_stream(in_audio):
            yield chunk

    async def wait_stream_ready(self, timeout: float) -> bool:
        assert self.stream_started.is_set()
        self.probe_timeout = timeout
        self.probe_started.set()
        if self.release is not None:
            await self.release.wait()
        self.probe_finished.set()
        if self.failure is not None:
            raise self.failure
        return self.ready

    async def close(self) -> None:
        self.close_called = True


class CloseRequiredConverter(VoiceConverter):
    """A stream that can end only when the bridge explicitly closes it."""

    def __init__(self) -> None:
        self.close_called = False
        self.closed = asyncio.Event()
        self.exited = asyncio.Event()

    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        async for _frame in in_audio:
            pass
        await self.closed.wait()
        self.exited.set()
        if False:
            yield b""

    async def close(self) -> None:
        self.close_called = True
        self.closed.set()


class StatsConverter(VoiceConverter):
    def __init__(self) -> None:
        self.on_stats = None

    async def convert_stream(self, _in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        assert self.on_stats is not None
        self.on_stats(
            {
                "infer_ms": 12.5,
                "model_version": "unit-model",
                "raw_audio": b"must-not-leak",
                "nested": {"audio": "must-not-leak"},
                "type": "not-client-controlled",
            }
        )
        if False:
            yield b""


VALID_CONFIG = {
    "type": "config",
    "sample_rate_in": 16000,
    "sample_rate_out": 48000,
    "frame_ms": 20,
}


def configured(incoming: list[bytes | BaseException]) -> list[dict | bytes | BaseException]:
    return [VALID_CONFIG, *incoming]


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
    websocket = FakeWebSocket(configured([sentinel, asyncio.CancelledError()]))
    converter = FakeConverter(output=bytes(1921))

    await run_bridge(websocket, converter)

    assert websocket.json_messages[0]["type"] == "ready"
    assert converter.inputs == [sentinel]
    assert websocket.binary_messages == [bytes(960), bytes(960)]
    assert all(sentinel not in message for message in websocket.binary_messages)


@pytest.mark.asyncio
async def test_bridge_sends_ready_only_after_converter_readiness_probe_succeeds():
    release = asyncio.Event()
    websocket = FakeWebSocket(configured([]))
    converter = ReadinessConverter(release=release)
    task = asyncio.create_task(DesktopAudioBridge(converter).run(websocket))

    await asyncio.wait_for(converter.probe_started.wait(), timeout=1)
    assert converter.stream_started.is_set()
    assert converter.probe_finished.is_set() is False
    assert websocket.json_messages == []
    assert converter.probe_timeout == 150.0

    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if websocket.json_messages:
            break
    assert websocket.json_messages[0] == {"type": "ready"}
    websocket.push(asyncio.CancelledError())
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("backend secret")])
async def test_bridge_fails_closed_when_converter_readiness_probe_fails(failure):
    websocket = FakeWebSocket(configured([bytes(640)]))
    converter = ReadinessConverter(
        ready=failure if isinstance(failure, bool) else True,
        failure=failure if isinstance(failure, BaseException) else None,
    )

    await asyncio.wait_for(DesktopAudioBridge(converter).run(websocket), timeout=1)

    assert websocket.json_messages == [
        {
            "type": "error",
            "code": "converter_unavailable",
            "message": "conversion backend unavailable",
        }
    ]
    assert websocket.binary_messages == []
    assert converter.inputs == []
    assert converter.close_called
    assert websocket.closed


@pytest.mark.asyncio
async def test_bridge_closes_when_client_disconnects_during_stream_readiness():
    websocket = FakeWebSocket(configured([asyncio.CancelledError()]))
    converter = ReadinessConverter(release=asyncio.Event())
    task = asyncio.create_task(DesktopAudioBridge(converter).run(websocket))

    await asyncio.wait_for(task, timeout=1)

    assert websocket.json_messages == []
    assert websocket.binary_messages == []
    assert converter.close_called
    assert websocket.closed


@pytest.mark.asyncio
async def test_bridge_cleans_up_when_cancelled_during_stream_readiness():
    websocket = FakeWebSocket(configured([]))
    converter = ReadinessConverter(release=asyncio.Event())
    task = asyncio.create_task(DesktopAudioBridge(converter).run(websocket))

    await asyncio.wait_for(converter.probe_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert websocket.json_messages == []
    assert websocket.binary_messages == []
    assert converter.close_called
    assert websocket.closed


@pytest.mark.asyncio
async def test_bridge_fails_closed_when_converter_readiness_probe_times_out():
    websocket = FakeWebSocket(configured([bytes(640)]))
    converter = ReadinessConverter(release=asyncio.Event())

    await asyncio.wait_for(
        DesktopAudioBridge(converter, readiness_timeout=0.01).run(websocket),
        timeout=1,
    )

    assert websocket.json_messages == [
        {
            "type": "error",
            "code": "converter_unavailable",
            "message": "conversion backend unavailable",
        }
    ]
    assert websocket.binary_messages == []
    assert converter.close_called
    assert websocket.closed


@pytest.mark.asyncio
async def test_bridge_rejects_malformed_input_without_converter_input():
    websocket = FakeWebSocket(configured([bytes(639), asyncio.CancelledError()]))
    converter = FakeConverter()

    await run_bridge(websocket, converter)

    assert converter.inputs == []
    assert any(message["type"] == "error" for message in websocket.json_messages)


@pytest.mark.asyncio
async def test_bridge_drops_oldest_input_when_queue_is_full():
    start = asyncio.Event()
    frames = [bytes([index]) * 640 for index in range(3)]
    websocket = FakeWebSocket(configured([*frames, asyncio.CancelledError()]))
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
    websocket = FakeWebSocket(configured([sentinel]))
    converter = FakeConverter(fail=True)

    await run_bridge(websocket, converter)

    types = [message["type"] for message in websocket.json_messages]
    assert "error" in types
    assert websocket.binary_messages == [silence_frame()]
    assert all(sentinel not in message for message in websocket.binary_messages)
    assert websocket.closed


@pytest.mark.asyncio
async def test_bridge_closes_converter_before_waiting_for_disconnect_shutdown():
    websocket = FakeWebSocket(configured([asyncio.CancelledError()]))
    converter = CloseRequiredConverter()

    await asyncio.wait_for(DesktopAudioBridge(converter).run(websocket), timeout=1)

    assert converter.close_called
    assert converter.exited.is_set()


@pytest.mark.asyncio
async def test_bridge_relays_sanitized_converter_stats():
    websocket = FakeWebSocket(configured([asyncio.CancelledError()]))
    converter = StatsConverter()

    await asyncio.wait_for(DesktopAudioBridge(converter).run(websocket), timeout=1)

    assert {
        "type": "stats",
        "infer_ms": 12.5,
        "model_version": "unit-model",
    } in websocket.json_messages
    assert all("raw_audio" not in message for message in websocket.json_messages)
    assert all("nested" not in message for message in websocket.json_messages)


@pytest.mark.asyncio
async def test_bridge_requires_config_before_ready_or_audio():
    websocket = FakeWebSocket([bytes(640)])
    converter = FakeConverter()

    await asyncio.wait_for(DesktopAudioBridge(converter).run(websocket), timeout=1)

    assert converter.inputs == []
    assert websocket.json_messages == [
        {"type": "error", "code": "invalid_config", "message": "expected JSON config"}
    ]
    assert websocket.closed


@pytest.mark.asyncio
async def test_bridge_rejects_wrong_config_sample_rates():
    config = {**VALID_CONFIG, "sample_rate_out": 16000}
    websocket = FakeWebSocket([config, bytes(640)])
    converter = FakeConverter()

    await asyncio.wait_for(DesktopAudioBridge(converter).run(websocket), timeout=1)

    assert converter.inputs == []
    assert websocket.json_messages[0]["type"] == "error"
    assert websocket.json_messages[0]["code"] == "invalid_config"
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


def test_local_no_auth_requires_explicit_loopback_only_flag(monkeypatch):
    from types import SimpleNamespace

    from backend import main

    loopback_request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        url=SimpleNamespace(scheme="http", netloc="127.0.0.1:8000", hostname="127.0.0.1", port=8000),
        headers={},
    )
    remote_request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        url=SimpleNamespace(scheme="https", netloc="kiera.example.com", hostname="kiera.example.com", port=443),
        headers={"x-forwarded-for": "203.0.113.10"},
    )

    monkeypatch.setattr(main, "LOCAL_NO_AUTH", False)
    assert main._local_no_auth_allowed(loopback_request) is False

    monkeypatch.setattr(main, "LOCAL_NO_AUTH", True)
    monkeypatch.setattr(main, "LOCAL_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(main, "LOCAL_LAUNCHER", True)
    monkeypatch.setattr(main, "LOCAL_NO_AUTH_ORIGIN", "http://127.0.0.1:8000")
    assert main._local_no_auth_allowed(loopback_request) is True
    assert main._local_no_auth_allowed(remote_request) is False
    monkeypatch.setattr(main, "LOCAL_NO_AUTH_ORIGIN", "https://127.0.0.1:8000")
    assert main._local_no_auth_allowed(loopback_request) is False


def test_local_no_auth_does_not_bypass_general_control_routes(monkeypatch):
    import asyncio

    from fastapi import HTTPException
    from backend import main

    monkeypatch.setattr(main, "LOCAL_NO_AUTH", True)
    monkeypatch.setattr(main, "LOCAL_BIND_HOST", "127.0.0.1")
    monkeypatch.setattr(main, "LOCAL_LAUNCHER", True)
    request = type(
        "RequestStub",
        (),
        {
            "client": type("Client", (), {"host": "127.0.0.1"})(),
            "url": type("URL", (), {"scheme": "http", "netloc": "127.0.0.1:8000", "hostname": "127.0.0.1", "port": 8000})(),
            "headers": {},
        },
    )()
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.require_control_token(request, ""))
    assert error.value.status_code in {401, 503}


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
