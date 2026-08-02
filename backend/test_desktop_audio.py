"""Tests for the desktop audio transport contracts and relay."""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
from fastapi import WebSocketDisconnect
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

    async def receive(self) -> dict:
        """ASGI-shaped receive, matching Starlette's WebSocket.receive().

        Queued bytes become a binary message, dicts become a text (JSON)
        message, and WebSocketDisconnect becomes the ASGI disconnect message
        instead of being raised -- exactly how a real socket reports a
        client hangup to this call.
        """
        message = await self._incoming.get()
        if isinstance(message, WebSocketDisconnect):
            return {"type": "websocket.disconnect", "code": message.code}
        if isinstance(message, BaseException):
            raise message
        if isinstance(message, dict):
            import json as _json
            return {"type": "websocket.receive", "text": _json.dumps(message)}
        return {"type": "websocket.receive", "bytes": message}

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


class TimestampingWebSocket(FakeWebSocket):
    """Records the wall-clock time of every binary send, to assert pacing."""

    def __init__(self, incoming) -> None:
        super().__init__(incoming)
        self.binary_send_times: list[float] = []

    async def send_bytes(self, message: bytes) -> None:
        self.binary_send_times.append(asyncio.get_running_loop().time())
        await super().send_bytes(message)


class BurstyConverter(VoiceConverter):
    """Reproduces the measured live Modal delivery pattern: a small burst,
    a long stall, then a flood of catch-up audio arriving far faster than
    real time. See the 2026-07-27 desktop investigation.
    """

    def __init__(self, *, burst_bytes: int, stall_seconds: float, flood_bytes: int) -> None:
        self.burst_bytes = burst_bytes
        self.stall_seconds = stall_seconds
        self.flood_bytes = flood_bytes
        self.finished = asyncio.Event()

    async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        consumer = asyncio.create_task(self._drain(in_audio))
        try:
            yield bytes(self.burst_bytes)
            await asyncio.sleep(self.stall_seconds)
            yield bytes(self.flood_bytes)
            self.finished.set()
            # Stay alive so teardown is driven by the caller, like the real
            # long-lived converter session.
            await asyncio.sleep(3600)
        finally:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer

    @staticmethod
    async def _drain(in_audio: AsyncIterator[bytes]) -> None:
        async for _frame in in_audio:
            pass


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


def test_desktop_session_issues_single_use_ticket_without_auth():
    from backend import main

    with TestClient(main.app) as client:
        response = client.post("/api/desktop/session", json={"profile": "female"})

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
        def __init__(self, converter, **kwargs) -> None:
            captured["bridge_converter"] = converter
            captured["bridge_kwargs"] = kwargs

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


@pytest.mark.asyncio
async def test_bursty_converter_output_is_paced_to_real_time():
    """A stalled-then-flooding converter must not be forwarded as a flood.

    Measured live 2026-07-27: Modal delivered 0.22s of audio, stalled ~8s, then
    dumped 6.2s of audio inside a single second. Forwarded unpaced, that is
    audible as a fraction of a second of speech followed by silence. Backlog
    must surface as delay, never as speed.
    """
    one_second = 96000  # 48kHz * 2 bytes
    # Deliberately NO WebSocketDisconnect: this test covers cancellation while
    # the client is still connected and listening, which is the only case where
    # dumping held backlog unpaced would be audible as time-compressed speech.
    # Once the client has actually hung up, teardown flushes instead (see
    # test_paced_playout_flushes_backlog_when_client_disconnects) -- gating that
    # flush on a disconnect the fixture itself performed is what made these two
    # behaviors look contradictory.
    websocket = TimestampingWebSocket(configured([bytes(640)] * 4))
    converter = BurstyConverter(
        burst_bytes=one_second // 4,
        stall_seconds=0.2,
        flood_bytes=one_second,
    )
    bridge = DesktopAudioBridge(converter, playout_cushion_bytes=one_second // 10)

    task = asyncio.create_task(bridge.run(websocket))
    await asyncio.wait_for(converter.finished.wait(), timeout=5)
    await asyncio.sleep(0.35)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    sent = len(websocket.binary_messages) * 960
    assert sent > 0, "expected some audio to be delivered"
    # 0.35s after the flood was produced, at most ~0.35s of it (plus the
    # cushion) may have been written. Unpaced, the whole 1s flood goes at once.
    assert sent < one_second, (
        f"converted audio was forwarded faster than real time: {sent} bytes "
        f"({sent / one_second:.2f}s of audio) written in ~0.35s"
    )


@pytest.mark.asyncio
async def test_backlog_above_catchup_threshold_shrinks_over_time():
    """Once real-time pacing falls behind, it must catch back up, not stay
    behind forever.

    Reproduced live 2026-08-02: a single early burst pushed held backlog past
    the cushion, and with strict 1x-only pacing that became a fixed ~4s delay
    for the rest of the call -- there was no mechanism to reduce backlog once
    it existed. This asserts the opposite: a large one-time flood must be
    delivered measurably faster than real time (bounded by
    PLAYOUT_CATCHUP_RATE) while catching up, not just eventually delivered.
    """
    one_second = 96000  # 48kHz * 2 bytes
    flood_seconds = 3.0
    websocket = TimestampingWebSocket(configured([bytes(640)] * 4))
    converter = BurstyConverter(
        burst_bytes=one_second // 10,
        stall_seconds=0.05,
        flood_bytes=int(one_second * flood_seconds),
    )
    bridge = DesktopAudioBridge(converter, playout_cushion_bytes=one_second // 10)

    task = asyncio.create_task(bridge.run(websocket))
    await asyncio.wait_for(converter.finished.wait(), timeout=5)
    # Let the pacer run long enough to observe catch-up behavior, well short
    # of the full flood_seconds a strict-1x pacer would need.
    await asyncio.sleep(1.5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    sent = len(websocket.binary_messages) * 960
    strict_real_time_bound = one_second * 1.5  # what 1.5s of wall-clock buys at 1x
    assert sent > strict_real_time_bound, (
        f"backlog did not catch up faster than real time: {sent} bytes "
        f"({sent / one_second:.2f}s of audio) delivered in ~1.5s wall-clock, "
        f"expected more than the {strict_real_time_bound / one_second:.2f}s "
        "a strict 1x pacer would deliver"
    )
    # Still bounded -- catch-up must not degrade into an unpaced dump. The
    # observed rate runs a bit above PLAYOUT_CATCHUP_RATE (event-loop/sleep
    # scheduling slack, not a pacing bug); this bound is generous enough to
    # tolerate that while still catching an unpaced/instant dump, which would
    # deliver close to the full 3s flood in 1.5s wall-clock.
    catchup_bound = one_second * 1.5 * 1.6
    assert sent < catchup_bound, (
        f"backlog caught up too fast, risking audible time-compression: "
        f"{sent} bytes ({sent / one_second:.2f}s) in ~1.5s wall-clock"
    )


@pytest.mark.asyncio
async def test_paced_playout_flushes_backlog_when_client_disconnects():
    """A client hangup must deliver held backlog, not discard it.

    Measured live 2026-07-29: the GPU produced 22.17s of converted audio and
    the browser received 1.8s. The converter is long-lived by design, so on a
    disconnect its stream never "ends" -- the task is torn down by cancellation
    instead, and the old teardown gate (`stream_ended_naturally`) therefore
    never fired, dropping everything the real-time pacer had not yet reached.

    Distinct from test_bursty_converter_output_is_paced_to_real_time: there the
    client is still connected, so pacing must be preserved. Here it has hung up,
    so there is no live playout left to protect.
    """
    one_second = 96000
    websocket = FakeWebSocket(configured([bytes(640), WebSocketDisconnect()]))

    produced = one_second * 3

    class LongLivedConverter(VoiceConverter):
        """Produces far faster than real time, then stays alive like production."""

        async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
            drain = asyncio.create_task(_consume(in_audio))
            try:
                for _ in range(3):
                    yield bytes(one_second)
                    await asyncio.sleep(0.01)
                await asyncio.sleep(3600)
            finally:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain

    async def _consume(in_audio: AsyncIterator[bytes]) -> None:
        async for _frame in in_audio:
            pass

    bridge = DesktopAudioBridge(
        LongLivedConverter(), playout_cushion_bytes=one_second // 10
    )
    task = asyncio.create_task(bridge.run(websocket))
    await asyncio.sleep(0.5)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    delivered = len(websocket.binary_messages) * 960
    assert delivered == produced, (
        f"expected all {produced} bytes delivered after client disconnect, "
        f"got {delivered} ({delivered / one_second:.2f}s of {produced / one_second:.2f}s)"
    )


@pytest.mark.asyncio
async def test_paced_playout_flushes_remaining_audio_on_teardown():
    """Real-time pacing means the bridge routinely holds genuine backlog.

    Tearing down must flush it rather than silently truncating the tail --
    the exact bug the LiveKit path hit when its pacer first landed
    (see .agents/context/subsystem-notes.md, 2026-07-21).
    """
    one_second = 96000
    websocket = FakeWebSocket(configured([bytes(640), WebSocketDisconnect()]))

    class ShortBurstConverter(VoiceConverter):
        async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
            async for _frame in in_audio:
                break
            yield bytes(one_second // 2)

    bridge = DesktopAudioBridge(
        ShortBurstConverter(), playout_cushion_bytes=one_second // 10
    )
    await asyncio.wait_for(bridge.run(websocket), timeout=5)

    delivered = len(websocket.binary_messages) * 960
    assert delivered == one_second // 2, (
        f"expected all {one_second // 2} bytes flushed on teardown, got {delivered}"
    )


@pytest.mark.asyncio
async def test_playout_starts_when_chunks_are_smaller_than_the_cushion():
    """Chunks smaller than the cushion must still accumulate and play.

    Regression: the consumer cleared `playout_ready` on every wake, including
    when it took no chunk, swallowing the producer's set() from the append that
    had just woken it. Playout then starved forever -- 23.5s of audio in, zero
    bytes out (measured locally 2026-07-27 before this fix).
    """
    one_second = 96000
    cushion = one_second // 4
    small_chunk = cushion // 5  # five chunks needed to reach the cushion
    websocket = FakeWebSocket(configured([bytes(640), WebSocketDisconnect()]))

    done = asyncio.Event()

    class SmallChunkConverter(VoiceConverter):
        async def convert_stream(self, in_audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
            drain = asyncio.create_task(_consume(in_audio))
            try:
                for _ in range(10):
                    yield bytes(small_chunk)
                    await asyncio.sleep(0.01)
                done.set()
                await asyncio.sleep(3600)  # long-lived, like the real session
            finally:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain

    async def _consume(in_audio: AsyncIterator[bytes]) -> None:
        async for _frame in in_audio:
            pass

    bridge = DesktopAudioBridge(
        SmallChunkConverter(), playout_cushion_bytes=cushion
    )
    task = asyncio.create_task(bridge.run(websocket))
    await asyncio.wait_for(done.wait(), timeout=10)
    # Give the paced consumer time to drain everything it holds.
    await asyncio.sleep(1.2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    delivered = len(websocket.binary_messages) * 960
    assert delivered == small_chunk * 10, (
        f"expected all {small_chunk * 10} bytes delivered, got {delivered}"
    )
