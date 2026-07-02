import asyncio
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
    """
    
    def __init__(
        self,
        room_url: str,
        token: str,
        converter: VoiceConverter,
        suppressor: NoiseSuppressor,
        chunk_duration_ms: int = 500,
        budget_ms: float = 300.0
    ):
        self.room_url = room_url
        self.token = token
        self.converter = converter
        self.suppressor = suppressor
        self.chunk_duration_ms = chunk_duration_ms
        self.budget_seconds = budget_ms / 1000.0
        
        self.room = rtc.Room()
        self.audio_source: Optional[rtc.AudioSource] = None
        self.published_track: Optional[rtc.LocalAudioTrack] = None
        
        self.running = False
        self._pipeline_task: Optional[asyncio.Task] = None
        self._conversion_task: Optional[asyncio.Task] = None
        self._audio_queue: Optional[asyncio.Queue] = None
        self._publish_lock: Optional[asyncio.Lock] = None

        # Pre-buffer: hold back audio until 1.5s of converted audio is ready,
        # then release it all at once so the lead hears smooth, uninterrupted speech.
        # The RVC pipeline needs ~1.4s anyway, so this delay is imperceptible.
        # 48000 samples/s × 2 bytes × 1.5s = 144000 bytes
        self._PREBUFFER_BYTES = 48000 * 2 * 1          # 1 second of 48kHz 16-bit mono
        self._prebuffer       = bytearray()
        self._prebuffer_ready = False   # True once buffer has filled for the first time

    async def start(self):
        """Starts the worker, connects to the LiveKit room, and publishes the output track."""
        self.running = True
        self._audio_queue = asyncio.Queue()
        self._publish_lock = asyncio.Lock()
        
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
        # We use 48kHz because RVC outputs 48kHz PCM.
        # Fallback audio (16kHz) will be resampled to 48kHz before publishing.
        # queue_size_ms bounds LiveKit's internal playout buffer. The default (~1s)
        # lets converted audio pile up mid-call, which is a major reason the lead's
        # delay kept *growing* to 10-15s. A small buffer keeps end-to-end latency flat.
        try:
            self.audio_source = rtc.AudioSource(48000, 1, queue_size_ms=400)
        except TypeError:
            # Older livekit builds don't accept queue_size_ms
            self.audio_source = rtc.AudioSource(48000, 1)
        self.published_track = rtc.LocalAudioTrack.create_audio_track(
            "converted-audio", 
            self.audio_source
        )
        await self.room.local_participant.publish_track(self.published_track)
        print("[Worker] Published converted-audio track to the room.")
        
        # Start the conversion consumer task
        self._conversion_task = asyncio.create_task(self._conversion_consumer())



    def stop_pipeline(self):
        """Stops the audio processing pipeline tasks."""
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
        # Reset pre-buffer so the next speech session starts fresh
        self._prebuffer.clear()
        self._prebuffer_ready = False
        print("[Worker] Pre-buffer reset for new speech session.")

    async def stop(self):
        """Disconnects the worker and stops all background tasks."""
        self.running = False
        self.stop_pipeline()

        if self._conversion_task:
            self._conversion_task.cancel()
            self._conversion_task = None

        if hasattr(self.converter, 'close'):
            await self.converter.close()

        if self.room.isconnected():
            await self.room.disconnect()
            print("[Worker] Disconnected from room.")

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

    async def _conversion_consumer(self):
        """
        VAD-aware parallel pipeline consumer.

        Instead of cutting audio every fixed N milliseconds (which splits sentences
        mid-word and makes the converted voice sound robotic/broken), this consumer
        uses Voice Activity Detection to find natural speech pauses and cuts chunks
        there — so each RVC call receives a complete phrase or sentence.

        Chunking rules:
          • Minimum chunk : 300ms  — avoids tiny bursts that confuse RVC
          • Cut trigger   : 200ms of consecutive silence detected by VAD
          • Maximum chunk : 2500ms — hard cap so latency never explodes on long sentences
          • Carry-over    : last 200ms of previous chunk prepended to next (smooths boundaries)

        Parallelism:
          Conversion fires in the background immediately after a chunk is cut.
          The next chunk is collected while the previous one is being converted.
          An ordered buffer ensures chunks are always published in the correct order.
        """
        SAMPLE_RATE   = 16000
        FRAME_BYTES   = 320          # 10ms @ 16kHz 16-bit mono
        FRAME_MS      = 10

        # Low-latency chunking. The old values (min 600 / max 2500 / silence 300)
        # meant that during continuous speech we waited up to 2.5s just to *collect*
        # a chunk before sending it to RVC — the dominant source of the 8-15s delay.
        # Smaller chunks cut collection latency to well under 1s. The 100ms carry-over
        # still smooths chunk boundaries so speech stays continuous ("no interruptions").
        MIN_CHUNK_MS  = 250          # don't send anything shorter to RVC (F0 needs some context)
        MAX_CHUNK_MS  = max(int(self.chunk_duration_ms), 400)  # hard cap, honors config (default 700ms)
        SILENCE_MS    = 150          # consecutive silence to trigger a natural-pause cut
        CARRY_MS      = 100          # carry-over prepended to the next chunk

        # Any chunk older than this (measured from when its first frame was captured)
        # is considered stale — we skip the RVC call / drop it rather than let the
        # lead's audio fall further behind. Keeps end-to-end latency bounded.
        MAX_AGE_S     = 1.0

        min_frames    = MIN_CHUNK_MS  // FRAME_MS
        max_frames    = MAX_CHUNK_MS  // FRAME_MS
        silence_limit = SILENCE_MS    // FRAME_MS

        carry_bytes   = int(SAMPLE_RATE * 2 * (CARRY_MS / 1000.0))   # carry-over bytes

        print(f"[Worker] VAD pipeline active. "
              f"min={MIN_CHUNK_MS}ms  silence_cut={SILENCE_MS}ms  max={MAX_CHUNK_MS}ms  "
              f"carry-over={CARRY_MS}ms  max_age={MAX_AGE_S}s  max_concurrent_rvc=2")

        # Try to initialise webrtcvad for silence detection (falls back to fixed chunking)
        vad = None
        try:
            import webrtcvad as _webrtcvad
            vad = _webrtcvad.Vad(2)   # aggressiveness 0-3 (2 = balanced)
            print("[Worker] VAD: webrtcvad initialised (aggressiveness=2)")
        except Exception as e:
            print(f"[Worker] VAD: webrtcvad unavailable ({e}) — falling back to fixed {MAX_CHUNK_MS}ms chunks")

        # Semaphore: cap concurrent RVC HTTP requests at 2
        rvc_semaphore = asyncio.Semaphore(2)
        # Lock ensures two chunks never interleave frames when publishing simultaneously
        publish_seq_lock = asyncio.Lock()
        pending_seq = 0

        async def convert_and_publish(chunk_with_carry: bytes, raw_chunk: bytes, carry_len: int, chunk_start_t: float, seq: int):
            """Convert one chunk and publish it immediately. No ordering required — a
            slight reorder is imperceptible on voice, and ordered buffering was the root
            cause of the cascading 26+ second latency backlog."""
            async with rvc_semaphore:
                # Skip RVC entirely if the chunk is already stale waiting in queue
                age_before = time.time() - chunk_start_t
                if age_before > MAX_AGE_S:
                    print(f"[Worker] Queue backed up: skipping RVC for stale chunk {seq} (age: {age_before:.1f}s)")
                    audio, is_converted = self._resample_16k_to_48k(raw_chunk), False
                else:
                    audio, is_converted = await self._convert_chunk(chunk_with_carry, raw_chunk, carry_len, chunk_start_t)

            # Re-check age after conversion — discard if too old to be useful
            age_after = time.time() - chunk_start_t
            if age_after > MAX_AGE_S * 3:
                print(f"[Worker] Dropping post-conversion stale chunk {seq} (age: {age_after:.1f}s)")
                return

            # Publish immediately — use lock so frames from parallel chunks don't interleave
            async with publish_seq_lock:
                await self._publish_audio(audio, is_converted=is_converted)

        publisher_task = None   # no longer used; publishing happens directly in convert_and_publish
        carry_over     = b""

        try:
            while self.running:
                try:
                    frames        = []          # raw 10ms frame bytes collected so far
                    silence_count = 0           # consecutive silent frames

                    while True:
                        t_in, frame_bytes = await self._audio_queue.get()
                        frames.append((t_in, frame_bytes))
                        self._audio_queue.task_done()

                        frame_count = len(frames)

                        # --- VAD silence detection ---
                        if vad is not None:
                            try:
                                is_speech = vad.is_speech(frame_bytes, SAMPLE_RATE)
                            except Exception:
                                is_speech = True   # assume speech on VAD error
                            silence_count = 0 if is_speech else silence_count + 1
                        else:
                            # No VAD: treat every frame as speech (fixed chunking by max_frames)
                            silence_count = 0

                        # Cut at natural pause (silence detected) once past minimum length
                        if frame_count >= min_frames and silence_count >= silence_limit:
                            print(f"[Worker] VAD cut at {frame_count * FRAME_MS}ms "
                                  f"(silence for {silence_count * FRAME_MS}ms)")
                            break

                        # Hard cut at maximum chunk length
                        if frame_count >= max_frames:
                            print(f"[Worker] Hard cut at {MAX_CHUNK_MS}ms (max chunk reached)")
                            break

                    chunk_start_t = frames[0][0]
                    pcm_chunk     = b"".join(fb for _, fb in frames)
                    chunk_bytes   = carry_over + pcm_chunk
                    carry_len     = len(carry_over)
                    carry_over    = pcm_chunk[-carry_bytes:] if len(pcm_chunk) >= carry_bytes else pcm_chunk

                    chunk_ms = len(frames) * FRAME_MS
                    print(f"[Worker] Dispatching chunk {pending_seq}: {chunk_ms}ms "
                          f"({len(chunk_bytes)} bytes incl. carry-over)")

                    # Fire RVC in background — immediately start collecting the next chunk
                    asyncio.create_task(convert_and_publish(chunk_bytes, pcm_chunk, carry_len, chunk_start_t, pending_seq))
                    pending_seq += 1

                    # Safety valve: shed frames when the input queue falls behind (> 2 seconds queued)
                    overload_frames = (2000 // FRAME_MS)
                    if self._audio_queue.qsize() > overload_frames:
                        shed = 0
                        while self._audio_queue.qsize() > overload_frames // 2:
                            try:
                                self._audio_queue.get_nowait()
                                self._audio_queue.task_done()
                                shed += 1
                            except asyncio.QueueEmpty:
                                break
                        print(f"[Worker] Overload: shed {shed} frames ({shed * FRAME_MS}ms)")

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    print(f"[Worker Error in consumer] {e}")
                    traceback.print_exc()
                    await asyncio.sleep(0.1)
        finally:
            if publisher_task:
                publisher_task.cancel()
                try:
                    await publisher_task
                except asyncio.CancelledError:
                    pass

    def _resample_16k_to_48k(self, pcm_16k: bytes) -> bytes:
        """Resample 16kHz 16-bit mono PCM to 48kHz by tripling each sample (numpy, ~10x faster)."""
        import numpy as np
        samples = np.frombuffer(pcm_16k, dtype=np.int16)
        return np.repeat(samples, 3).tobytes()

    async def _convert_chunk(self, chunk_with_carry: bytes, raw_chunk: bytes, carry_len: int, chunk_start_t: float) -> tuple[bytes, bool]:
        """Converts one chunk via the voice converter.
        Returns (48kHz PCM, is_converted) where is_converted=False means fail-safe raw audio."""
        async def chunk_generator() -> AsyncIterator[bytes]:
            yield chunk_with_carry

        converted_bytes = bytearray()
        start_request_t = time.time()
        success = False

        try:
            async with asyncio.timeout(self.budget_seconds):
                async for converted_chunk in self.converter.convert_stream(chunk_generator()):
                    converted_bytes.extend(converted_chunk)
                success = len(converted_bytes) > 0
        except Exception as e:
            elapsed_ms = (time.time() - start_request_t) * 1000.0
            error_type = "TIMEOUT" if isinstance(e, (asyncio.TimeoutError, TimeoutError)) else "ERROR"
            print(f"[Worker WARNING] Conversion {error_type} after {elapsed_ms:.1f}ms: {e}. Triggering fail-safe!")

        added_latency_ms = (time.time() - chunk_start_t) * 1000.0

        if success:
            print(f"[Latency] {added_latency_ms:.0f}ms total (conversion: {(time.time()-start_request_t)*1000:.0f}ms)")
            # Slice off the converted carry-over context to prevent a repeating stutter
            # at chunk boundaries. RVC does NOT guarantee the output length is exactly
            # 3× the input length (it resamples internally and F0/pitch processing shifts
            # sample counts), so a fixed carry_len*3 slice mis-cuts the boundary and clips
            # words or leaks duplicated syllables. Slice *proportionally* to the real
            # output length so the boundary stays aligned regardless of the output rate.
            total_in = len(chunk_with_carry)
            if total_in > 0 and carry_len > 0:
                slice_len = int(round(len(converted_bytes) * (carry_len / total_in)))
                slice_len -= slice_len % 2  # keep 16-bit sample alignment
            else:
                slice_len = 0
            audio_payload = bytes(converted_bytes[slice_len:])

            # Emit latency metric to the room data channel for the frontend
            if self.room and self.room.isconnected():
                import json
                async def safe_publish():
                    try:
                        await self.room.local_participant.publish_data(
                            json.dumps({"pipeline_latency_ms": added_latency_ms, "is_fallback": False}).encode()
                        )
                    except Exception:
                        pass
                asyncio.create_task(safe_publish())

            return audio_payload, True
        else:
            print(f"[Worker] Fail-safe fallback (latency: {added_latency_ms:.0f}ms)")
            # Fallback resamples the raw chunk without the carry-over context
            audio_payload = self._resample_16k_to_48k(raw_chunk)
            return audio_payload, False


    async def _publish_audio(self, audio_payload: bytes, is_converted: bool = True):
        """Publish 48kHz PCM audio to LiveKit with a pre-buffer for smooth playback.

        The first PREBUFFER_BYTES of *converted* audio are held back. Once the buffer fills,
        the full buffer is released as a burst and streaming continues normally.
        Fail-safe (raw, unconverted) chunks are silently discarded during the pre-buffer phase
        so the lead only ever hears Kiera's voice — never a leaking raw syllable.
        Once streaming has started, fail-safe audio is accepted normally to prevent gaps.
        """
        async with self._publish_lock:
            if not self._prebuffer_ready:
                if not is_converted:
                    # Discard fail-safe audio while pre-buffering — only real converted
                    # audio should prime the buffer. This prevents the first syllable
                    # ('Hey,') from leaking through in the original raw voice.
                    print("[Worker] Pre-buffer: discarding fail-safe chunk (not converted).")
                    return

                # Add converted audio to the pre-buffer
                self._prebuffer.extend(audio_payload)
                buffered_secs = len(self._prebuffer) / (48000 * 2)
                print(f"[Worker] Pre-buffering... {buffered_secs:.2f}s / {self._PREBUFFER_BYTES/(48000*2):.1f}s")

                if len(self._prebuffer) >= self._PREBUFFER_BYTES:
                    # Buffer is full — release everything and switch to streaming
                    self._prebuffer_ready = True
                    audio_payload = bytes(self._prebuffer)
                    self._prebuffer.clear()
                    print("[Worker] Pre-buffer full — starting smooth playback to lead.")
                else:
                    return  # Still filling, don't publish yet

            # Normal frame-by-frame publish
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
