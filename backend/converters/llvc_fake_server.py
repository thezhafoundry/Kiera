import asyncio
import json
import logging
import numpy as np
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

active_sessions = 0
active_sessions_lock = asyncio.Lock()


class StatefulUpsampler:
    def __init__(self):
        self.last_sample = 0

    def process(self, frame_bytes: bytes) -> bytes:
        frame = np.frombuffer(frame_bytes, dtype=np.int16)
        N = len(frame)
        if N == 0:
            return b""
        
        # Prepend last sample to construct temporary array of length N + 1
        temp = np.concatenate(([self.last_sample], frame))
        
        # Linearly interpolate factor 3
        # out[3*j] = temp[j]
        # out[3*j+1] = (2*temp[j] + temp[j+1]) // 3
        # out[3*j+2] = (temp[j] + 2*temp[j+1]) // 3
        out = np.zeros(3 * N, dtype=np.int16)
        
        prev = temp[:-1]
        nxt = temp[1:]
        
        out[0::3] = prev
        out[1::3] = (2 * prev.astype(np.int32) + nxt.astype(np.int32)) // 3
        out[2::3] = (prev.astype(np.int32) + 2 * nxt.astype(np.int32)) // 3
        
        self.last_sample = frame[-1]
        return out.tobytes()


class StatefulCausalModel:
    def __init__(self, carrier_freq: float = 250.0, sample_rate: float = 48000.0):
        self.carrier_freq = carrier_freq
        self.sample_rate = sample_rate
        self.phase = 0.0
        self.phase_step = 2 * np.pi * carrier_freq / sample_rate

    def process(self, audio_bytes: bytes) -> bytes:
        samples = np.frombuffer(audio_bytes, dtype=np.int16)
        N = len(samples)
        if N == 0:
            return b""
        
        t = np.arange(N)
        angles = self.phase + t * self.phase_step
        
        # Apply ring modulation
        modulated = (samples * np.sin(angles)).astype(np.int16)
        
        self.phase = (angles[-1] + self.phase_step) % (2 * np.pi)
        return modulated.tobytes()


async def llvc_fake_ws_handler(websocket):
    global active_sessions
    
    async with active_sessions_lock:
        if active_sessions >= 2:
            logger.warning("[LLVC Fake Server] Connection rejected: busy (2 sessions active)")
            await websocket.send(json.dumps({"type": "busy", "reason": "Max sessions (2) reached"}))
            await asyncio.sleep(0.1)
            await websocket.close()
            return
        active_sessions += 1

    logger.info(f"[LLVC Fake Server] Session started. Active sessions: {active_sessions}")
    
    upsampler = StatefulUpsampler()
    causal_model = StatefulCausalModel()
    frames_received = 0

    try:
        # 1. Config Handshake
        config_msg = await websocket.recv()
        # Expecting JSON config
        try:
            config = json.loads(config_msg)
        except (json.JSONDecodeError, TypeError):
            logger.error("[LLVC Fake Server] Handshake failed: malformed JSON config")
            await websocket.send(json.dumps({"type": "error", "message": "Malformed config"}))
            await websocket.close()
            return

        # Send ready response
        await websocket.send(json.dumps({"type": "ready"}))

        # 2. Main Processing Loop
        async for message in websocket:
            if isinstance(message, (bytes, bytearray)):
                # Stateful upsampling 16 -> 48 kHz
                upsampled = upsampler.process(message)
                # Stateful ring modulator
                modulated = causal_model.process(upsampled)
                
                # Send back 48kHz audio chunk
                await websocket.send(modulated)
                
                # Send stats every 16 frames (320ms)
                frames_received += 1
                if frames_received >= 16:
                    stats_payload = {
                        "type": "stats",
                        "infer_ms": 3.5,
                        "block_ms": 320,
                        "total_ms": 3.5,
                    }
                    await websocket.send(json.dumps(stats_payload))
                    frames_received = 0
            else:
                # Handle ping messages or other client controls
                try:
                    data = json.loads(message)
                    if data.get("type") == "ping":
                        await websocket.send(json.dumps({"type": "pong"}))
                except Exception:
                    pass

    except ConnectionClosed:
        logger.info("[LLVC Fake Server] Connection closed by client")
    except Exception as e:
        logger.error(f"[LLVC Fake Server] Error: {e}")
    finally:
        async with active_sessions_lock:
            active_sessions -= 1
        logger.info(f"[LLVC Fake Server] Session ended. Active sessions: {active_sessions}")
