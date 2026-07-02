import asyncio
import collections
import contextlib
import json
import time
import traceback
from typing import AsyncIterator, Optional
from livekit import rtc

from .converters.base import VoiceConverter
from .noise.noise_suppressor import NoiseSuppressor


class VoiceConversionWorker:
    """
    LiveKit client worker that manages the audio processing loop:
    agent mic -> LiveKit -> WebRTC Noise Suppressor -> Voice Converter (RVC) -> LiveKit -> Listener.

    The converter is driven as ONE long-lived duplex stream for the life of the
    worker's active pipeline: denoised 20ms frames are fed in continuously and
    converted 48kHz audio is republished continuously, in the order it arrives.
    Because everything now travels over a single ordered TCP/WS stream there is
    nothing to reorder — the old VAD-chunking / parallel-RVC-request / reorder
    buffer machinery is gone.

    NEVER publishes raw/unconverted audio. If the converter's backend connection
    drops, output is simply silence (nothing published) until it reconnects and
    resumes yielding real converted audio — there is no raw-audio fallback path.
    """

    # One-time jitter-buffer fill target before playout starts: hold converted
    # output until this many bytes (100ms of 48kHz 16-bit mono) have accumulated,
    # then publish continuously. After the initial fill, capture_frame's own
    # backpressure paces playback — see _publish_frames / AudioSource queue_size_ms.
    _JITTER_TARGET_BYTES = int(48000 * 2 * 0.1)

    # If no converted audio has arrived for this long, the data-channel latency
    # metric reports is_fallback=True ("HOLDING") so the existing frontend badge
    # reflects an outage/reconnect. Purely observational — never affects the
    # actual audio path.
    _HOLD_TIMEOUT_S = 0.75

    # Bytes of 48kHz 16-bit mono PCM equivalent to one 20ms (640-byte, 16kHz)
    # input frame, used to align the local timestamp-based latency estimate (the
    # fallback used when the converter has no on_stats hook, e.g. DummyVoiceConverter)
    # with arriving output bytes.
    _OUTPUT_BYTES_PER_INPUT_FRAME = 640 * 3

    def __init__(
        self,
        room_url: str,
        token: str,
        converter: VoiceConverter,
        suppressor: NoiseSuppressor,
    ):
        self.room_url = room_url
        self.token = token
        self.converter = converter
        self.suppressor = suppressor

        self.room = rtc.Room()
        self.audio_source: Optional[rtc.AudioSource] = None
        self.published_track: Optional[rtc.LocalAudioTrack] = None

        self.running = False
        self._pipeline_task: Optional[asyncio.Task] = None
        self._conversion_task: Optional[asyncio.Task] = None
        self._audio_queue: Optional[asyncio.Queue] = None

        # Fail-closed warm-gate cache (Task 4): background probe result, cheap to
        # read on every poll (e.g. /api/call/wait every ~3s) unlike wait_until_ready
        # itself, which opens a real probe connection to the converter's backend.
        self._ready: bool = False
        self._readiness_task: Optional[asyncio.Task] = None

        # Data-channel latency/fallback metric state (frontend/app.js reads
        # {"pipeline_latency_ms":.., "is_fallback":..} unchanged — see
        # _publish_latency_metric).
        self._latest_latency_ms: float = 0.0
        self._is_holding: bool = True          # True until real converted audio has flowed
        self._last_chunk_at: float = 0.0        # monotonic time of the last output chunk
        self._last_metric_publish_at: float = 0.0

        # Timestamps of frames sent into the converter (20ms/640-byte each), used
        # only as a local latency estimate when the converter has no richer stats
        # hook (see on_stats wiring below and _estimate_fallback_latency_ms).
        self._frame_sent_at: collections.deque = collections.deque()
        self._pending_output_bytes: int = 0

        # RVCStreamingConverter exposes on_stats({"infer_ms":.., "block_ms":..});
        # DummyVoiceConverter (and the offline-only RVCVoiceConverter) don't. When
        # present, server-reported stats are the latency source of truth; when
        # absent, fall back to the local send-timestamp estimate.
        self._use_stats_latency = hasattr(self.converter, "on_stats")
        if self._use_stats_latency:
            self.converter.on_stats = self._on_converter_stats

    async def start(self):
        """Starts the worker, connects to the LiveKit room, and publishes the output track."""
        self.running = True
        self._audio_queue = asyncio.Queue()

        # Define event handlers
        @self.room.on("track_subscribed")
        def on_track_subscribed(track, publication, participant):
            if isinstance(track, rtc.RemoteAudioTrack):
                # We subscribe to any participant whose identity contains 'agent'
                if "agent" in participant.identity.lower():
                    print(f"[Worker] Subscribed to agent track {track.sid} from participant {participant.identity}")
                    # Start the audio processing pipeline for this track
                    self._pipeline_task = asyncio.create_task(self._run_audio_pipeline(track))

        @self.room.on("track_published")
        def on_track_published(publication, participant):
            """Explicitly subscribe to any audio track published by an agent participant.
            Without this, track_subscribed never fires because LiveKit requires
            explicit subscription when auto_subscribe is not enabled."""
            if "agent" in participant.identity.lower():
                print(f"[Worker] Agent published track {publication.sid} — subscribing explicitly...")
                publication.set_subscribed(True)

        @self.room.on("track_unsubscribed")
        def on_track_unsubscribed(track, publication, participant):
            if "agent" in participant.identity.lower():
                print(f"[Worker] Agent track {track.sid} unsubscribed. Stopping pipeline.")
                self.stop_pipeline()

        @self.room.on("participant_connected")
        def on_participant_connected(participant):
            """When a new agent joins after the bot, subscribe to their existing tracks."""
            if "agent" in participant.identity.lower():
                print(f"[Worker] Agent participant connected: {participant.identity}")
                for publication in participant.track_publications.values():
                    if publication.kind == rtc.TrackKind.KIND_AUDIO:
                        print(f"[Worker] Subscribing to existing agent track {publication.sid}")
                        publication.set_subscribed(True)

        # Connect to room
        print(f"[Worker] Connecting to room: {self.room_url}...")
        await self.room.connect(self.room_url, self.token)
        print(f"[Worker] Connected. Identity: {self.room.local_participant.identity}")

        # Scan for any agent participants already in the room before the bot joined
        print("[Worker] Scanning for existing agent participants in room...")
        for participant in self.room.remote_participants.values():
            if "agent" in participant.identity.lower():
                print(f"[Worker] Found existing agent: {participant.identity} — subscribing to their tracks...")
                for publication in participant.track_publications.values():
                    if publication.kind == rtc.TrackKind.KIND_AUDIO:
                        print(f"[Worker] Subscribing to existing agent track {publication.sid}")
                        await publication.set_subscribed(True)

        # Publish the converted audio output track
        # We use 48kHz because the converter (RVC-style engines) outputs 48kHz PCM.
        # queue_size_ms bounds LiveKit's internal playout buffer. Lowered from the
        # old 400ms to 200ms: the new consumer has its own small (100ms, one-shot)
        # jitter buffer upstream of this, so a large LiveKit-side buffer on top of
        # that would just add latency without absorbing any additional jitter —
        # 200ms is enough headroom for capture_frame's pacing without letting
        # converted audio pile up mid-call.
        try:
            self.audio_source = rtc.AudioSource(48000, 1, queue_size_ms=200)
        except TypeError:
            # Older livekit builds don't accept queue_size_ms
            self.audio_source = rtc.AudioSource(48000, 1)
        self.published_track = rtc.LocalAudioTrack.create_audio_track(
            "converted-audio",
            self.audio_source
        )
        await self.room.local_participant.publish_track(self.published_track)
        print("[Worker] Published converted-audio track to the room.")

        # Drive the converter as one long-lived duplex stream for the life of the
        # worker's active pipeline. Frames arrive back in order on this single
        # stream, so there is no separate ordering/playout task anymore.
        self._conversion_task = asyncio.create_task(self._run_conversion_stream())

    def stop_pipeline(self):
        """Stops the producer reading from the agent's LiveKit track (called on
        track_unsubscribed). The conversion stream itself is intentionally NOT
        touched here: it is a single long-lived duplex connection to the converter
        for the life of the worker (see start()/stop()), and tearing it down and
        reconnecting per track event would be wasteful (re-pays GPU/session
        warm-up) for no benefit. With the producer stopped, _frame_pairs simply
        idles on an empty queue until a track resubscribes."""
        if self._pipeline_task:
            self._pipeline_task.cancel()
            self._pipeline_task = None
        # Clear audio queue
        if self._audio_queue:
            while not self._audio_queue.empty():
                try:
                    self._audio_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def stop(self):
        """Disconnects the worker and stops all background tasks."""
        self.running = False
        self.stop_pipeline()

        if self._conversion_task:
            self._conversion_task.cancel()
            try:
                await self._conversion_task
            except asyncio.CancelledError:
                pass
            self._conversion_task = None

        if self._readiness_task:
            self._readiness_task.cancel()
            try:
                await self._readiness_task
            except asyncio.CancelledError:
                pass
            self._readiness_task = None

        if hasattr(self.converter, 'close'):
            await self.converter.close()

        if self.room.isconnected():
            await self.room.disconnect()
            print("[Worker] Disconnected from room.")

    @property
    def is_ready(self) -> bool:
        """Cheap, synchronous readiness read for high-frequency callers (e.g.
        Twilio polling /api/call/wait every ~3s). Reflects the result of the
        one-shot background probe started by start_readiness_probe(); False
        until that probe resolves True."""
        return self._ready

    def start_readiness_probe(self, timeout: float):
        """Kicks off a single background task that calls wait_until_ready() once
        and caches the result in self._ready for the life of this worker. Must
        be called at most once per worker (called from _do_start_bot right after
        construction). Cancelled alongside _conversion_task in stop()."""
        if self._readiness_task is not None:
            return

        async def _probe():
            try:
                self._ready = await self.wait_until_ready(timeout)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[Worker] Readiness probe failed: {e}")
                self._ready = False

        self._readiness_task = asyncio.create_task(_probe())

    async def wait_until_ready(self, timeout: float) -> bool:
        """Fail-closed warm-gate probe (used by the caller before dialing/bridging
        a lead): confirms the converter's backend can accept a session within
        `timeout` seconds. Converters without a real "warm" concept (e.g.
        DummyVoiceConverter) are always considered ready."""
        wait_ready = getattr(self.converter, "wait_ready", None)
        if wait_ready is None:
            return True
        return await wait_ready(timeout)

    async def _run_audio_pipeline(self, track: rtc.RemoteAudioTrack):
        """Producer loop: Receives raw mic frames, applies noise suppression, and pushes to queue."""
        # Request 16kHz mono PCM frames from LiveKit to match the pipeline rate
        stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        print("[Worker] Started reading remote audio stream @ 16kHz mono")

        try:
            async for frame_event in stream:
                frame = frame_event.frame
                # frame.data is 16-bit mono PCM. At 10ms, this is exactly 160 samples (320 bytes).

                # 1. Apply Noise Suppression (WebRTC or RNNoise)
                # frame.data returns a memoryview of int16 — convert to raw bytes for the suppressor
                denoised_bytes = self.suppressor.process_frame(bytes(frame.data))

                # 2. Push to queue along with the ingress timestamp
                if self._audio_queue:
                    await self._audio_queue.put((time.time(), denoised_bytes))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[Worker Error in pipeline] {e}")
            traceback.print_exc()
        finally:
            await stream.aclose()
            print("[Worker] Audio stream closed")

    async def _frame_pairs(self) -> AsyncIterator[bytes]:
        """Pulls denoised 10ms (320-byte) frames off _audio_queue and pairs two
        consecutive frames into a single 20ms (640-byte) frame — the input frame
        size the streaming converter protocol expects. Runs for the life of the
        conversion stream; when the producer isn't running (no agent track
        currently subscribed) this just idles on an empty queue rather than
        ending, since the same converter session is reused across track
        subscribe/unsubscribe events within one call.
        """
        while True:
            t1, first = await self._audio_queue.get()
            self._audio_queue.task_done()
            _, second = await self._audio_queue.get()
            self._audio_queue.task_done()
            # Record the send timestamp (of the earlier of the two frames) for the
            # local fallback latency estimate — see _estimate_fallback_latency_ms.
            self._frame_sent_at.append(t1)
            yield first + second

    def _on_converter_stats(self, stats: dict):
        """Registered as converter.on_stats. Called (synchronously, from whatever
        task is driving the converter's receive loop) whenever the backend reports
        timing for a processed block. This is the latency source of truth for
        converters that support it (RVCStreamingConverter)."""
        infer_ms = stats.get("infer_ms") or 0.0
        block_ms = stats.get("block_ms") or 0.0
        self._latest_latency_ms = float(infer_ms) + float(block_ms)
        self._publish_latency_metric()

    def _estimate_fallback_latency_ms(self, converted_chunk: bytes) -> Optional[float]:
        """Local latency estimate used only when the converter has no on_stats hook
        (e.g. DummyVoiceConverter, or the offline-only RVCVoiceConverter): consumes
        send timestamps off _frame_sent_at in lockstep with arriving output bytes
        (3x the input byte count, since output is 48kHz vs 16kHz input) and returns
        elapsed time since the oldest still-in-flight frame was sent."""
        self._pending_output_bytes += len(converted_chunk)
        latest_ts = None
        while (self._pending_output_bytes >= self._OUTPUT_BYTES_PER_INPUT_FRAME
               and self._frame_sent_at):
            latest_ts = self._frame_sent_at.popleft()
            self._pending_output_bytes -= self._OUTPUT_BYTES_PER_INPUT_FRAME
        if latest_ts is None:
            return None
        return (time.time() - latest_ts) * 1000.0

    def _publish_latency_metric(self, force: bool = False):
        """Fire-and-forget publish of the latency/fallback badge JSON to the room
        data channel. Same shape the frontend has always parsed
        (frontend/app.js ~L382) — is_fallback doubles as a "HOLDING" indicator
        while the converter is disconnected/reconnecting (see _HOLD_TIMEOUT_S)."""
        now = time.monotonic()
        if not force and (now - self._last_metric_publish_at) < 0.2:
            return
        self._last_metric_publish_at = now

        if not (self.room and self.room.isconnected()):
            return
        payload = json.dumps({
            "pipeline_latency_ms": self._latest_latency_ms,
            "is_fallback": self._is_holding,
        }).encode()

        async def safe_publish():
            try:
                await self.room.local_participant.publish_data(payload)
            except Exception:
                pass
        asyncio.create_task(safe_publish())

    async def _holding_watchdog(self):
        """Runs alongside the conversion stream. Flips is_holding back to True (and
        republishes the latency metric so the frontend badge reacts promptly) if no
        converted audio has arrived for _HOLD_TIMEOUT_S — e.g. the converter's WS
        dropped and is reconnecting. Purely observational: never touches the
        converter or the audio path itself."""
        try:
            while True:
                await asyncio.sleep(0.2)
                if not self._is_holding and (time.monotonic() - self._last_chunk_at) > self._HOLD_TIMEOUT_S:
                    self._is_holding = True
                    self._publish_latency_metric(force=True)
        except asyncio.CancelledError:
            pass

    async def _run_conversion_stream(self):
        """Drives the converter as ONE long-lived duplex stream for the life of the
        worker's active pipeline: feeds 20ms frames in via _frame_pairs and
        republishes whatever comes back, in arrival order — a single ordered
        stream has nothing to reorder. Never falls back to raw audio: on outage
        the converter simply stops yielding and output is silence until it
        reconnects and resumes (see RVCStreamingConverter's own bounded
        reconnect-buffer/backoff logic in converters/rvc_stream.py).
        """
        gen = self.converter.convert_stream(self._frame_pairs())
        jitter_buffer = bytearray()
        jitter_filled = False
        self._last_chunk_at = time.monotonic()
        watchdog_task = asyncio.create_task(self._holding_watchdog())

        try:
            # contextlib.aclosing guarantees the async generator is properly closed
            # (triggering the converter's internal teardown) whenever we leave this
            # block — whether by exhausting it, an exception, or this task being
            # cancelled. A bare `async for ... break` would not reliably do this.
            async with contextlib.aclosing(gen):
                async for converted_chunk in gen:
                    self._last_chunk_at = time.monotonic()
                    if self._is_holding:
                        self._is_holding = False
                        self._publish_latency_metric(force=True)

                    if not self._use_stats_latency:
                        estimate = self._estimate_fallback_latency_ms(converted_chunk)
                        if estimate is not None:
                            self._latest_latency_ms = estimate
                            self._publish_latency_metric()

                    if not converted_chunk:
                        continue

                    if not jitter_filled:
                        jitter_buffer.extend(converted_chunk)
                        if len(jitter_buffer) >= self._JITTER_TARGET_BYTES:
                            jitter_filled = True
                            await self._publish_frames(bytes(jitter_buffer))
                            jitter_buffer.clear()
                    else:
                        await self._publish_frames(converted_chunk)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[Worker Error in conversion stream] {e}")
            traceback.print_exc()
        finally:
            watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog_task

    async def _publish_frames(self, audio_payload: bytes):
        """Slice 48kHz PCM into 10ms frames and push them to LiveKit."""
        frame_size = 960  # 10ms at 48kHz mono 16-bit
        for i in range(0, len(audio_payload), frame_size):
            slice_bytes = audio_payload[i:i + frame_size]
            if len(slice_bytes) < frame_size:
                slice_bytes = slice_bytes + b'\x00' * (frame_size - len(slice_bytes))
            audio_frame = rtc.AudioFrame(
                data=slice_bytes,
                sample_rate=48000,
                num_channels=1,
                samples_per_channel=480,
            )
            try:
                await self.audio_source.capture_frame(audio_frame)
            except Exception:
                break
