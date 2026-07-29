import os
import asyncio
import contextlib
import datetime
import secrets
import time
from typing import Literal, Optional
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    Form,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from livekit import api
from .converters.rvc_stream import RVCStreamingConverter
from .converters.dummy import DummyVoiceConverter
from .noise.noise_suppressor import WebRTCNoiseSuppressor
from .pipeline import VoiceConversionWorker
from .security import (
    redact_phone_number,
    validate_agent_identity,
    validate_e164_phone,
    verify_bearer_token,
    RateLimiter,
)

# Load environment variables
load_dotenv()

app = FastAPI(title="Keira MVP Voice Conversion Server")

# Operator control-plane authentication. Fail closed when this is absent: a
# public deployment must never silently fall back to unauthenticated controls.
CONTROL_PLANE_TOKEN = os.getenv("KEIRA_CONTROL_TOKEN") or os.getenv("CONTROL_PLANE_TOKEN", "")

# Small per-process rate limiter. This is a first guard, not a replacement for
# a shared gateway limiter when the service is scaled beyond one instance.
_RATE_LIMIT_WINDOW_SECONDS = 60.0
_RATE_LIMIT_DEFAULT = 120
_RATE_LIMIT_EXPENSIVE = 10
_EXPENSIVE_PATHS = {"/api/setup", "/api/deploy", "/api/warmup"}
_rate_limiter = RateLimiter(window_seconds=_RATE_LIMIT_WINDOW_SECONDS)


def _rate_limit_allows(client_key: str, path: str) -> bool:
    limit = _RATE_LIMIT_EXPENSIVE if path in _EXPENSIVE_PATHS else _RATE_LIMIT_DEFAULT
    return _rate_limiter.allows(
        client_key,
        path if path in _EXPENSIVE_PATHS else "api",
        limit,
    )


@app.middleware("http")
async def rate_limit_api(request: Request, call_next):
    if request.url.path.startswith("/api/") and not _rate_limit_allows(
        request.client.host if request.client else "unknown",
        request.url.path,
    ):
        return JSONResponse({"detail": "Too many requests. Try again later."}, status_code=429)
    return await call_next(request)


@app.middleware("http")
async def disable_static_asset_cache(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.endswith((".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


async def require_control_token(authorization: str = Header(default="")):
    if not CONTROL_PLANE_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Control-plane authentication is not configured.",
        )
    if not verify_bearer_token(authorization, CONTROL_PLANE_TOKEN):
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_twilio_signature(request: Request):
    """Validate Twilio's signed callback before accepting form data."""
    if not TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Twilio callback validation is not configured.")
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status_code=403, detail="Invalid Twilio callback signature.")

    from twilio.request_validator import RequestValidator

    form_values = dict(await request.form())
    url = f"{SERVER_URL.rstrip('/')}{request.url.path}" if SERVER_URL else str(request.url)
    if request.url.query:
        url = f"{url}?{request.url.query}"
    if not RequestValidator(TWILIO_AUTH_TOKEN).validate(url, form_values, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio callback signature.")


def _validate_room_name(value: str) -> str:
    if not value or len(value) > 128 or not all(c.isalnum() or c in "_.-" for c in value):
        raise ValueError("invalid room name")
    return value


def _require_agent_identity(value: str) -> None:
    if not validate_agent_identity(value):
        raise HTTPException(
            status_code=422,
            detail="identity must be 5-64 safe characters and include 'agent'.",
        )


class _WorkerStartState:
    def __init__(self):
        self.event = asyncio.Event()
        self.error: Optional[str] = None


worker_tasks: dict[str, asyncio.Task] = {}
worker_start_states: dict[str, _WorkerStartState] = {}
isolation_events: dict[str, asyncio.Event] = {}


def _spawn_observed_task(coro, *, name: str) -> asyncio.Task:
    """Create a background task and always retrieve/log its terminal exception."""
    task = asyncio.create_task(coro, name=name)

    def observe_result(done: asyncio.Task) -> None:
        if done.cancelled():
            return
        try:
            error = done.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            print(f"[Server] Background task {name} failed: {type(error).__name__}")

    task.add_done_callback(observe_result)
    return task


@app.on_event("startup")
async def _on_startup():
    global _rvc_keepwarm_task
    _rvc_keepwarm_task = _spawn_observed_task(
        _rvc_keepwarm_loop(),
        name="keira-rvc-keepwarm",
    )
    print("[Server] RVC keep-warm loop started (pings every 90s).")


# Configure CORS
# allow_origins=["*"] and allow_credentials=True cannot be combined (browsers reject it).
# Use explicit origin list for credentialed requests; fall back to "*" only for non-credentialed.
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=(CORS_ORIGINS != ["*"]),
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")

# RVC serverless GPU configuration
RVC_ENDPOINT_URL = os.getenv("RVC_ENDPOINT_URL")
RVC_API_KEY = os.getenv("RVC_API_KEY", "")
ALLOW_DUMMY_CONVERTER = os.getenv("ALLOW_DUMMY_CONVERTER", "false").lower() == "true"
RVC_MODEL_VERSION = os.getenv("RVC_MODEL_VERSION", "mi-test")
RVC_PITCH_SHIFT = int(os.getenv("RVC_PITCH_SHIFT", "0"))
# Blend between FAISS-retrieved target-voice timbre (1.0) and raw HuBERT content
# features (0.0), which still carry some of the original speaker's own vocal-tract
# characteristics. Bumped from RVCStreamingConverter's 0.75 default (2026-07-03
# experiment: live streamed calls sound less like the trained voice than the offline
# `modal run` one-shot test, which uses the same 0.75 default -- so this alone isn't
# confirmed as the cause, but it's cheap/reversible to test). Env var, not another
# code deploy, so it can be re-tuned from Render without touching code again.
RVC_INDEX_RATE = float(os.getenv("RVC_INDEX_RATE", "0.9"))
# Loudness-contour blend: 1.0 = model's own (near-constant) loudness, 0.0 = follow the
# agent's delivery. The 0.75 server default flattens speech dynamics (measured 2026-07-08:
# input modulation depth 0.99 → output 0.62), which reads as mushy/"blabbering"; 0.25
# recovered it to 0.80 with identity/pitch unchanged. Env-tunable; default keeps the
# server's legacy 0.75.
RVC_RMS_MIX_RATE = float(os.getenv("RVC_RMS_MIX_RATE", "0.75"))
# Voiceless-consonant protection (lower = stronger protection, 0.5 disables). 0.2 keeps
# t/s/k crisper through conversion; server legacy default is 0.33.
RVC_PROTECT = float(os.getenv("RVC_PROTECT", "0.33"))
# Semitone shift applied for a "male" agent so their voice lands in the trained
# model's pitch range. The old fixed +12 doubles a ~137Hz male fundamental to
# ~274Hz — ~5 semitones ABOVE the mi-test model's ~208Hz center (measured
# 2026-07-08: +12 output F0 271Hz vs 208Hz good reference), which pushes RVC
# outside its trained range and produces a wrong-identity, muffled voice. Per-agent
# F0 differs, so this is env-tunable (default keeps the legacy +12); set to 7-8 for
# a ~137Hz agent. A "female" agent stays at 0.
RVC_MALE_PITCH_SHIFT = int(os.getenv("RVC_MALE_PITCH_SHIFT", "12"))
# Adaptive per-call pitch lock (spec: docs/superpowers/specs/2026-07-13-adaptive-pitch-shift-design.md).
# The fixed RVC_MALE_PITCH_SHIFT above goes stale whenever the agent's natural
# delivery shifts (2026-07-13: live agent F0 152-158Hz vs the 137-138Hz that +7
# was calibrated on -> output landed 1.5-2 st above the model center = the
# documented wrong-identity voice). When enabled, the GPU worker measures the
# live voiced F0 (median over >=2s of voiced RMVPE frames) and locks
# shift = 12*log2(RVC_TARGET_F0 / median) once per call; the gender-toggle
# shift becomes only the pre-lock prior. Set to 0 to disable (exact legacy
# fixed-shift behavior).
RVC_ADAPTIVE_PITCH = os.getenv("RVC_ADAPTIVE_PITCH", "1") == "1"
# The trained model's F0 center in Hz that the adaptive lock targets
# (mi-test: ~208, measured 2026-07-08 from a known-good output).
RVC_TARGET_F0 = float(os.getenv("RVC_TARGET_F0", "208"))

DUMMY_MODEL_VERSION = "dummy-development"
# WebRTC noise-suppression aggressiveness [0..4]. Level 3 was gutting high-frequency
# voice detail (sibilance/consonants HuBERT needs) BEFORE the model saw it —
# measured 2026-07-08: live input centroid 413Hz vs 720Hz clean, -9dB at 6-8kHz —
# the dominant cause of the "muffled" converted output. Level 1 is the validated
# clarity-preserving default; NS_LEVEL retains the deployment override.
NS_LEVEL = int(os.getenv("NS_LEVEL", "1"))

# Twilio Configuration
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
# TWILIO_SIP_TRUNK_ID: LiveKit SIP Outbound Trunk ID (created via /api/setup).
# Not a Twilio resource ID — generated by LiveKit when you configure it to call out via Twilio.
TWILIO_SIP_TRUNK_ID = os.getenv("TWILIO_SIP_TRUNK_ID")
# TWILIO_SIP_URI: Termination URI of your Twilio Elastic SIP Trunk (e.g. myproject.pstn.twilio.com).
# Used once by /api/setup to create the LiveKit outbound trunk. Not needed at call time.
TWILIO_SIP_URI = os.getenv("TWILIO_SIP_URI")
# SIP credentials matching the Twilio Elastic SIP Trunk Credential List (set once via /api/setup)
TWILIO_SIP_USERNAME = os.getenv("TWILIO_SIP_USERNAME", "Keira")
# Do NOT hard-code a fallback password — require it to be set explicitly in env vars.
TWILIO_SIP_PASSWORD = os.getenv("TWILIO_SIP_PASSWORD", "")
# Public URL of this server — used by /api/setup to configure Twilio's inbound webhook.
SERVER_URL = os.getenv("SERVER_URL")

# Initialize Twilio Client optionally
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        from twilio.rest import Client
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception as e:
        print(f"[Server Warning] Failed to initialize Twilio client: {e}")

# Global tracking
active_workers = {}
active_calls = {}

# Background keep-warm task handle
_rvc_keepwarm_task: Optional[asyncio.Task] = None


async def _rvc_keepwarm_loop():
    """Pings the Modal RVC /health endpoint every 90 seconds to keep the GPU
    container warm between calls. Modal's scaledown_window=120s means the
    container shuts down after 2 minutes of inactivity — pinging every 90s
    keeps it alive so calls never hit a cold start.
    Timeout is 150s to cover Modal cold-start time (~60-90s).

    Disabled by default to avoid 24/7 GPU billing (~$550-600/mo on the L4).
    Enable by setting RVC_KEEPWARM=1 before backend startup. On Render, changing the
    environment normally restarts the service, which is when this loop reads the setting.
    """
    # Phase D decision (2026-07-06): env-gated to save cost; default OFF.
    if os.getenv("RVC_KEEPWARM", "0") != "1":
        return
    if not RVC_ENDPOINT_URL:
        return
    health_url = (
        RVC_ENDPOINT_URL
        .replace("/convert", "/health")
        .replace("web-convert", "web-health")
        .replace("web_convert", "web_health")
    )
    import httpx
    while True:
        try:
            async with httpx.AsyncClient(timeout=150.0) as client:
                resp = await client.get(health_url)
            print(f"[Server] RVC keep-warm ping → {resp.status_code} (container warm)")
        except Exception as e:
            print(f"[Server] RVC keep-warm ping failed (non-fatal): {e}")
        await asyncio.sleep(90)



async def _restrict_sip_audio(room_name: str, sip_identity: str = "sip-lead"):
    """
    Option A — Server-side SIP track isolation.

    LiveKit's SIP bridge runs server-side and ignores browser-level
    setTrackSubscriptionPermissions(), so the SIP participant always receives
    a mix of EVERY audio track in the room — including the agent's raw mic.
    The lead would hear the real agent voice on top of the converted Keira voice.

    This helper uses LiveKit Room Service update_subscriptions() to explicitly
    unsubscribe the SIP participant from the raw agent mic track so they ONLY
    hear the bot's converted-audio track.

    For outbound calls sip_identity is always "sip-lead" (set explicitly in
    create_sip_participant). For inbound calls, LiveKit assigns a dynamic identity
    from the caller's SIP URI — so we detect the SIP participant by exclusion:
    whoever is NOT a voice-converter-bot and NOT an agent browser participant.

    Retries up to max_attempts times to handle the window where the agent browser
    and/or SIP participant hasn't joined yet.
    """
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        return False

    max_attempts = 20
    retry_delay = 0.25  # keep the lead held behind the gate while participants settle

    for attempt in range(1, max_attempts + 1):
        await asyncio.sleep(retry_delay)
        lk = None
        try:
            lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            resp = await lk.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )

            # Categorise every participant in the room
            raw_agent_track_sids = []
            bot_track_sids = []
            resolved_sip_identity = None

            for p in resp.participants:
                identity = p.identity
                is_bot = identity.startswith("voice-converter-bot")
                is_agent = "agent" in identity.lower() and not is_bot
                # SIP participant: matches the known identity OR is neither bot nor agent
                is_sip = (identity == sip_identity) or \
                          (not is_bot and not is_agent and identity != sip_identity and
                           sip_identity not in ("sip-lead",))  # dynamic inbound detection

                # For outbound: trust the explicit sip_identity
                if identity == sip_identity:
                    resolved_sip_identity = identity

                # For inbound: the SIP participant identity is dynamic
                if sip_identity == "sip-caller" and not is_bot and not is_agent:
                    resolved_sip_identity = identity

                for t in p.tracks:
                    if t.type == 0:  # 0 = AUDIO
                        if is_bot:
                            bot_track_sids.append(t.sid)
                        elif is_agent:
                            raw_agent_track_sids.append(t.sid)

            if not resolved_sip_identity:
                print(f"[SIP Isolation] Attempt {attempt}: SIP participant not yet in room — retrying...")
                continue

            if not raw_agent_track_sids:
                print(f"[SIP Isolation] Attempt {attempt}: No raw agent tracks found yet — retrying...")
                continue

            # Unsubscribe SIP participant from the raw agent mic track(s)
            await lk.room.update_subscriptions(
                api.UpdateSubscriptionsRequest(
                    room=room_name,
                    identity=resolved_sip_identity,
                    track_sids=raw_agent_track_sids,
                    subscribe=False,
                )
            )
            print(
                f"[SIP Isolation] ✅ '{resolved_sip_identity}' unsubscribed from raw agent "
                f"tracks {raw_agent_track_sids}. "
                f"Only bot converted tracks {bot_track_sids} will reach the phone."
            )
            return True

        except Exception as e:
            print(f"[SIP Isolation] Attempt {attempt} failed: {e}")
        finally:
            if lk:
                await lk.aclose()

    print(f"[SIP Isolation] ⚠️ Could not restrict SIP subscriptions for room {room_name} after {max_attempts} attempts.")
    return False


# Log Twilio configuration state at startup so misconfigurations are obvious in logs
def _log_config():
    print("=== Keira Server Configuration ===")
    print(f"  LiveKit URL:          {LIVEKIT_URL}")
    print(f"  LiveKit API Key:      {'SET' if LIVEKIT_API_KEY else 'MISSING ⚠️'}")
    print(f"  LiveKit API Secret:   {'SET' if LIVEKIT_API_SECRET else 'MISSING ⚠️'}")
    print(f"  Twilio Account SID:   {'SET' if TWILIO_ACCOUNT_SID else 'MISSING ⚠️'}")
    print(f"  Twilio Auth Token:    {'SET' if TWILIO_AUTH_TOKEN else 'MISSING ⚠️'}")
    print(f"  Twilio Phone Number:  {TWILIO_PHONE_NUMBER or 'MISSING ⚠️'}")
    print(f"  Twilio SIP URI:       {TWILIO_SIP_URI or 'not set (needed for /api/setup)'}")
    print(f"  LiveKit SIP Trunk ID: {TWILIO_SIP_TRUNK_ID or 'MISSING ⚠️  → run POST /api/setup to create'}")
    print(f"  Server URL:           {SERVER_URL or 'not set (needed for /api/setup webhook config)'}")
    print(
        "  RVC Endpoint:         "
        f"{RVC_ENDPOINT_URL or 'not set (PSTN calls fail closed)'}"
    )
    print("==================================")

_log_config()

# WebSocket connection manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket, *, already_accepted: bool = False):
        if not already_accepted:
            await websocket.accept()
        self.active_connections.append(websocket)
        print(f"[WebSockets] Agent connected. Total agents: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"[WebSockets] Agent disconnected. Total agents: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"[WebSockets Error] Failed to send message: {e}")

manager = ConnectionManager()

# Helper to derive the LiveKit SIP domain from LIVEKIT_URL
def get_livekit_sip_domain() -> str:
    from urllib.parse import urlparse
    try:
        parsed = urlparse(LIVEKIT_URL)
        host = parsed.netloc or parsed.path
        host = host.split(":")[0]  # strip port
        parts = host.split(".")
        if len(parts) >= 3 and parts[-2] == "livekit" and parts[-1] == "cloud":
            return f"{parts[0]}.sip.livekit.cloud"
    except Exception:
        pass
    return "sip.livekit.cloud"

# Shared logic to poll the RVC endpoint's /health until the Modal GPU reports "ready",
# used both by the manual /api/warmup endpoint and to block an outbound dial until the
# voice engine can actually convert audio. Never raises — always returns a status dict.
async def _wait_for_rvc_ready(max_wait_seconds: float = 360.0, poll_interval: float = 30.0) -> dict:
    if not RVC_ENDPOINT_URL:
        return {"status": "skipped", "message": "No RVC endpoint URL configured."}

    import httpx
    health_url = RVC_ENDPOINT_URL.replace("/convert", "/health").replace("web-convert", "web-health").replace("web_convert", "web_health")
    print(f"[RVC Warmup] Polling {health_url} (max_wait={max_wait_seconds:.0f}s, interval={poll_interval:.0f}s)")

    elapsed = 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(35.0, connect=10.0)) as client:
        while elapsed < max_wait_seconds:
            try:
                resp = await client.get(health_url)
            except httpx.TimeoutException:
                elapsed += poll_interval
                print(f"[RVC Warmup] GPU not ready yet (timeout after {poll_interval:.0f}s). Retrying... ({elapsed:.0f}/{max_wait_seconds:.0f}s elapsed)")
                continue

            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "loading":
                    elapsed += poll_interval
                    print(f"[RVC Warmup] GPU booting, model loading... Retrying... ({elapsed:.0f}/{max_wait_seconds:.0f}s elapsed)")
                    await asyncio.sleep(poll_interval)
                    continue
                print(f"[RVC Warmup] SUCCESS: {data}")
                return {"status": "success", "message": "GPU warmed up successfully.", "rvc_status": data}
            else:
                print(f"[RVC Warmup] FAILED: HTTP Status {resp.status_code} - {resp.text}")
                return {"status": "error", "message": f"RVC endpoint returned status code {resp.status_code}"}

    print(f"[RVC Warmup] TIMEOUT: GPU did not become ready within {max_wait_seconds:.0f}s")
    return {"status": "error", "message": f"GPU did not become ready within {max_wait_seconds:.0f}s."}

# Shared lifecycle helpers for converter bots
async def _wait_for_worker_started(room_name: str, timeout: float = 30.0) -> None:
    state = worker_start_states.get(room_name)
    if state is None:
        raise HTTPException(status_code=500, detail="Bot startup state is missing.")
    try:
        await asyncio.wait_for(state.event.wait(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Voice bot did not join LiveKit in time.") from exc
    if state.error:
        raise HTTPException(status_code=503, detail="Voice bot failed to join LiveKit.")


async def _cleanup_room_state(
    room_name: str,
    *,
    delete_room: bool = False,
    remove_call: bool = True,
) -> None:
    worker = active_workers.pop(room_name, None)
    task = worker_tasks.pop(room_name, None)
    worker_start_states.pop(room_name, None)
    isolation_events.pop(room_name, None)

    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if worker:
        await worker.stop()

    if delete_room and LIVEKIT_API_KEY and LIVEKIT_API_SECRET:
        lk = None
        try:
            lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception as exc:
            print(f"[Server] Room cleanup failed for {room_name}: {type(exc).__name__}")
        finally:
            if lk:
                await lk.aclose()
    if remove_call:
        active_calls.pop(room_name, None)


# Shared logic to spawn a converter bot for a room
async def _do_start_bot(
    room_name: str,
    agent_gender: str = "male",
    requested_engine: str = "rvc",
    *,
    require_real_engine: bool = False,
):
    _validate_room_name(room_name)
    if room_name in active_workers:
        return {"status": "already_running", "message": f"Voice conversion bot is already active in room {room_name}."}

    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(status_code=500, detail="LiveKit server keys are not configured.")

    # Generate token specifically for the bot participant
    bot_identity = f"voice-converter-bot-{room_name}"
    bot_token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
        .with_identity(bot_identity) \
        .with_name("Voice Conversion Bot") \
        .with_grants(api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True
        )) \
        .with_ttl(datetime.timedelta(seconds=3600))
    
    effective_engine = requested_engine
    fallback_reason: Optional[str] = None

    if effective_engine == "rvc" and not RVC_ENDPOINT_URL:
        if require_real_engine or not ALLOW_DUMMY_CONVERTER:
            raise HTTPException(
                status_code=503,
                detail="A real RVC voice engine is not configured.",
            )
        effective_engine = "dummy"
        fallback_reason = fallback_reason or "Development dummy converter explicitly enabled"

    print(f"[Server] Requested engine: {requested_engine} -> Effective: {effective_engine} (Fallback reason: {fallback_reason})")

    # Choose voice conversion engine
    if effective_engine == "rvc":
        pitch_shift = RVC_MALE_PITCH_SHIFT if agent_gender.lower() == "male" else 0
        print(f"[Server] Spawning RVC Streaming Voice Changer (Endpoint: {RVC_ENDPOINT_URL}, Pitch Prior: {pitch_shift})")
        converter = RVCStreamingConverter(
            endpoint_url=RVC_ENDPOINT_URL,
            api_key=RVC_API_KEY,
            pitch_shift=pitch_shift,
            index_rate=RVC_INDEX_RATE,
            rms_mix_rate=RVC_RMS_MIX_RATE,
            protect=RVC_PROTECT,
            adaptive_pitch=RVC_ADAPTIVE_PITCH,
            target_f0=RVC_TARGET_F0,
            connect_timeout=150.0,  # allow full Modal cold-start window (~60-90s)
            model_version=RVC_MODEL_VERSION,
        )
        model_version = RVC_MODEL_VERSION
    elif effective_engine == "dummy":
        print("[Server] Spawning explicitly enabled development Dummy Pitch Modulation Engine.")
        converter = DummyVoiceConverter()
        model_version = DUMMY_MODEL_VERSION
    else:
        raise HTTPException(status_code=503, detail="No permitted voice engine is available.")

    # Initialize noise suppression (WebRTC). Level via NS_LEVEL env — lowering it
    # preserves the high-frequency voice detail the RVC model needs for a clear,
    # on-identity conversion (see NS_LEVEL definition above).
    suppressor = WebRTCNoiseSuppressor(ns_level=NS_LEVEL)

    worker = VoiceConversionWorker(
        room_url=LIVEKIT_URL,
        token=bot_token.to_jwt(),
        converter=converter,
        suppressor=suppressor,
        call_id=room_name,
        requested_engine=requested_engine,
        effective_engine=effective_engine,
        fallback_reason=fallback_reason,
        model_version=model_version,
    )

    active_workers[room_name] = worker

    # Fail-closed warm gate (Task 4): kick off a single background readiness probe
    # for the life of this worker. Twilio polls /api/call/wait every ~3s while a
    # caller is on hold, so worker.is_ready must be a cheap property read — not a
    # fresh network probe per poll. This background task calls the (potentially
    # slow) wait_until_ready() exactly once and caches the result.
    worker.start_readiness_probe(150.0)

    # Start bot in a managed task immediately. FastAPI BackgroundTasks begin only
    # after the HTTP response, which was too late for readiness-gated dialing.
    start_state = _WorkerStartState()
    worker_start_states[room_name] = start_state

    async def run_worker_task():
        try:
            await worker.start()
            start_state.event.set()
            while worker.running:
                await asyncio.sleep(1.0)
        except Exception as e:
            start_state.error = type(e).__name__
            start_state.event.set()
            print(f"[Server Error running bot] {e}")
        finally:
            active_workers.pop(room_name, None)
            worker_tasks.pop(room_name, None)
            if worker.running or worker.room.isconnected():
                await worker.stop()
            print(f"[Server] Bot cleaned up for room: {room_name}")

    # Kick off a GPU warm-up ping the instant the bot starts. Modal cold-starts the
    # Modal L4 on the first request (cold-start timing varies); firing /health now overlaps that cold start
    # with call setup and the lead's phone ringing, so the GPU is (nearly) warm by the
    # time the agent's first words arrive — instead of the cold start being added on
    # top of the first converted audio. Fire-and-forget; failures are non-fatal.
    if RVC_ENDPOINT_URL:
        async def _prewarm_rvc():
            try:
                import httpx
                health_url = (RVC_ENDPOINT_URL
                              .replace("/convert", "/health")
                              .replace("web-convert", "web-health")
                              .replace("web_convert", "web_health"))
                async with httpx.AsyncClient(timeout=120.0) as client:
                    await client.get(health_url)
                print("[Server] RVC pre-warm ping sent on bot start (overlaps GPU cold start with ringing)")
            except Exception as e:
                print(f"[Server] RVC pre-warm ping failed (non-fatal): {e}")
        _spawn_observed_task(
            _prewarm_rvc(),
            name=f"keira-rvc-prewarm-{room_name}",
        )

    worker_tasks[room_name] = _spawn_observed_task(
        run_worker_task(),
        name=f"keira-worker-{room_name}",
    )
    return {"status": "started", "botIdentity": bot_identity}


async def _ensure_pstn_worker_ready(room_name: str, agent_gender: str):
    """Return a ready worker."""
    worker = active_workers.get(room_name)
    if worker is None:
        raise HTTPException(status_code=503, detail="Voice worker is unavailable.")

    if await worker.wait_for_readiness_probe(150.0):
        return worker

    raise HTTPException(status_code=503, detail="Voice engine is not ready.")


def _persist_engine_state(call_info: dict, worker) -> None:
    call_info.update({
        "requested_engine": worker.requested_engine,
        "effective_engine": worker.effective_engine,
        "fallback_reason": worker.fallback_reason or "",
        "model_version": worker.model_version,
    })


class TokenRequest(BaseModel):
    roomName: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    identity: str = Field(min_length=5, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    agentGender: Literal["male", "female"] = "male"
    voiceEngine: Literal["rvc"] = "rvc"

@app.post("/api/token", dependencies=[Depends(require_control_token)])
async def get_token(request: TokenRequest):
    """
    Brokers LiveKit client access tokens for browser web clients.
    """
    _require_agent_identity(request.identity)
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(
            status_code=500, 
            detail="LiveKit API keys are not configured on the server."
        )
        
    try:
        token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
            .with_identity(request.identity) \
            .with_name(f"Participant {request.identity}") \
            .with_grants(api.VideoGrants(
                room_join=True,
                room=request.roomName,
                can_publish=True,
                can_subscribe=True
            )) \
            .with_ttl(datetime.timedelta(seconds=3600))

        return {"token": token.to_jwt(), "serverUrl": LIVEKIT_URL}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/start-bot", dependencies=[Depends(require_control_token)])
async def start_bot(request: TokenRequest):
    """
    Spawns the real-time audio pipeline bot and connects it to the specified LiveKit room.
    """
    _require_agent_identity(request.identity)
    return await _do_start_bot(request.roomName, request.agentGender, request.voiceEngine)

@app.post("/api/stop-bot", dependencies=[Depends(require_control_token)])
async def stop_bot(request: TokenRequest):
    """
    Forces the voice conversion bot in a specific room to disconnect and clean up.
    """
    room_name = request.roomName
    worker = active_workers.get(room_name)
    if worker:
        await _cleanup_room_state(room_name)
        return {"status": "stopped", "message": f"Voice conversion bot stopped in room {room_name}."}
    return {"status": "not_running", "message": f"No active bot found in room {room_name}."}

# --- TWILIO & SIP INTEGRATION ENDPOINTS ---

class OutboundCallRequest(BaseModel):
    phoneNumber: str = Field(pattern=r"^\+\d{8,15}$")
    agentIdentity: str = Field(min_length=5, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    agentGender: Literal["male", "female"] = "male"
    voiceEngine: Literal["rvc"] = "rvc"


class OutboundDialRequest(BaseModel):
    roomName: str = Field(min_length=1, max_length=128, pattern=r"^outbound_[A-Za-z0-9_.-]+$")
    phoneNumber: str = Field(pattern=r"^\+\d{8,15}$")


def _new_outbound_room_name(phone_number: str) -> str:
    """Return a safe room ID with 96 bits of collision-resistant randomness."""
    return f"outbound_{phone_number[1:]}_{secrets.token_hex(12)}"


async def _find_outbound_trunk():
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        return None
    lk = None
    try:
        lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        trunks = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
        trunk = next((t for t in trunks.items if t.name == "Keira Twilio Outbound"), None)
        return trunk.sip_trunk_id if trunk else TWILIO_SIP_TRUNK_ID
    except Exception as exc:
        print(f"[Server] Live trunk lookup failed: {type(exc).__name__}")
        return TWILIO_SIP_TRUNK_ID
    finally:
        if lk:
            await lk.aclose()


async def _agent_has_audio_track(room_name: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lk = None
        try:
            lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            participants = await lk.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            for participant in participants.participants:
                if "agent" not in participant.identity.lower():
                    continue
                if any(track.type == 0 for track in participant.tracks):
                    return True
        except Exception as exc:
            print(f"[Server] Agent-track check failed: {type(exc).__name__}")
        finally:
            if lk:
                await lk.aclose()
        await asyncio.sleep(0.25)
    return False


@app.post("/api/call/outbound", dependencies=[Depends(require_control_token)])
async def call_outbound(request: OutboundCallRequest):
    """Prepare a room and bot; the browser joins before the SIP leg is dialed."""
    _require_agent_identity(request.agentIdentity)
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(status_code=500, detail="LiveKit API keys are not configured.")

    room_name = _new_outbound_room_name(request.phoneNumber)
    try:
        await _do_start_bot(
            room_name,
            request.agentGender,
            request.voiceEngine,
            require_real_engine=True,
        )
        await _wait_for_worker_started(room_name)
        worker = await _ensure_pstn_worker_ready(room_name, request.agentGender)
        active_calls[room_name] = {
            "room_name": room_name,
            "from": redact_phone_number(TWILIO_PHONE_NUMBER),
            "to": request.phoneNumber,
            "status": "ready-to-dial",
            "direction": "outbound",
            "agent_identity": request.agentIdentity,
            "agent_gender": request.agentGender,
            "created_at": datetime.datetime.now().isoformat(),
        }
        _persist_engine_state(active_calls[room_name], worker)
    except HTTPException:
        await _cleanup_room_state(room_name, delete_room=True)
        raise
    except Exception:
        await _cleanup_room_state(room_name, delete_room=True)
        raise HTTPException(status_code=503, detail="Unable to prepare the voice call.")

    agent_token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
        .with_identity(request.agentIdentity) \
        .with_name(f"Agent {request.agentIdentity}") \
        .with_grants(api.VideoGrants(room_join=True, room=room_name, can_publish=True, can_subscribe=True)) \
        .with_ttl(datetime.timedelta(seconds=3600))
    return {
        "status": "ready_to_dial",
        "roomName": room_name,
        "token": agent_token.to_jwt(),
        "serverUrl": LIVEKIT_URL,
        "requested_engine": worker.requested_engine,
        "effective_engine": worker.effective_engine,
        "fallback_reason": worker.fallback_reason or "",
        "model_version": worker.model_version,
    }


@app.post("/api/call/outbound/dial", dependencies=[Depends(require_control_token)])
async def dial_outbound(request: OutboundDialRequest):
    """Dial only after the agent track exists, then confirm SIP isolation."""
    call_info = active_calls.get(request.roomName)
    if not call_info or call_info.get("status") != "ready-to-dial":
        raise HTTPException(status_code=404, detail="Prepared outbound call not found.")
    if call_info.get("to") != request.phoneNumber:
        raise HTTPException(status_code=409, detail="Outbound call details do not match.")
    if not await _agent_has_audio_track(request.roomName):
        await _cleanup_room_state(request.roomName, delete_room=True)
        raise HTTPException(status_code=503, detail="Agent audio track was not ready.")

    try:
        worker = await _ensure_pstn_worker_ready(
            request.roomName,
            call_info.get("agent_gender", "male"),
        )
    except HTTPException:
        await _cleanup_room_state(request.roomName, delete_room=True)
        raise
    _persist_engine_state(call_info, worker)

    sip_trunk_id = await _find_outbound_trunk()
    if not sip_trunk_id:
        await _cleanup_room_state(request.roomName, delete_room=True)
        raise HTTPException(status_code=503, detail="No SIP outbound trunk is configured.")

    lk = None
    try:
        lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        result = await lk.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=sip_trunk_id,
                sip_call_to=request.phoneNumber,
                room_name=request.roomName,
                participant_identity="sip-lead",
                participant_name="Lead",
            )
        )
        print(f"[Server] SIP participant created: id={result.participant_id}")
    except Exception as exc:
        print(f"[Server] SIP dial failed: {type(exc).__name__}")
        await _cleanup_room_state(request.roomName, delete_room=True)
        raise HTTPException(status_code=502, detail="Unable to initiate the SIP call.") from exc
    finally:
        if lk:
            await lk.aclose()

    isolated = await _restrict_sip_audio(request.roomName, sip_identity="sip-lead")
    if not isolated:
        await _cleanup_room_state(request.roomName, delete_room=True)
        raise HTTPException(status_code=502, detail="SIP audio isolation could not be confirmed.")
    isolation_events.setdefault(request.roomName, asyncio.Event()).set()
    call_info["status"] = "in-progress"
    return {"status": "initiated", "roomName": request.roomName}

@app.post("/api/call/status-event")
async def call_status_event(
    request: Request,
    CallSid: str = Form(default=""),
    CallStatus: str = Form(default=""),
    CallDuration: str = Form(default=""),
    To: str = Form(default=""),
    From: str = Form(default=""),
    ErrorCode: str = Form(default=""),
    ErrorMessage: str = Form(default=""),
):
    """Twilio status callback — logs every call state change so we can diagnose failures."""
    await require_twilio_signature(request)
    print(f"[Twilio Status] SID={CallSid} status={CallStatus} duration={CallDuration}s to={To} from={From} error={ErrorCode} {ErrorMessage}")
    return Response(status_code=204)

@app.post("/api/call/inbound")
async def call_inbound(
    request: Request,
    From: str = Form(...),
    CallSid: str = Form(...),
    To: str = Form(default=""),
    CallStatus: str = Form(default=""),
):
    """
    Twilio voice webhook for inbound calls.

    Flow:
      1. Return TwiML that puts caller on hold (plays ringing tone) and polls /api/call/wait
         every 3 seconds to check if agent has accepted.
      2. Agent accepts → /api/call/accept is called → active_calls[room]["status"] = "accepted"
      3. /api/call/wait detects acceptance → returns TwiML that bridges to LiveKit SIP.

    This avoids the previous bug where Twilio bridged to LiveKit before LiveKit was ready,
    causing an immediate SIP rejection and 2-4 second drops.
    """
    await require_twilio_signature(request)
    print(f"[Twilio Inbound] CallSid={CallSid} From={redact_phone_number(From)} Status={CallStatus}")
    room_name = f"inbound_{CallSid}"

    # 1. Hold the caller and defer bot creation until the operator accepts with
    # an explicit voice profile.
    isolation_events[room_name] = asyncio.Event()

    # 2. Record the inbound call
    active_calls[room_name] = {
        "room_name": room_name,
        "from": From,
        "to": To or TWILIO_PHONE_NUMBER or "Keira Inbound",
        "status": "ringing",
        "direction": "inbound",
        "call_sid": CallSid,
        "created_at": datetime.datetime.now().isoformat()
    }

    # 3. Broadcast incoming call to WebSockets (agent UI shows popup)
    await manager.broadcast({
        "event": "incoming_call",
        "roomName": room_name,
        "callerId": From,
        "callSid": CallSid
    })
    print(f"[Twilio Inbound] Broadcasted incoming_call event for room={room_name}")

    # 4. Return TwiML: put caller on hold and poll /api/call/wait until agent accepts
    wait_url = f"{SERVER_URL.rstrip('/') if SERVER_URL else ''}/api/call/wait?callSid={CallSid}"
    status_callback = f"{SERVER_URL.rstrip('/') if SERVER_URL else ''}/api/call/status-event"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Pause length="60"/>
    <Hangup/>
</Response>"""
    # If SERVER_URL is set, use Redirect polling. Without a public callback URL we
    # cannot validate Twilio requests or safely gate the SIP bridge, so fail closed
    # instead of reviving the old immediate-bridge behavior.
    if SERVER_URL:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Redirect method="POST">{wait_url}</Redirect>
</Response>"""
    else:
        print("[Twilio Inbound] SERVER_URL is not configured; refusing an ungated SIP bridge")
    print(f"[Twilio Inbound] Returning TwiML for CallSid={CallSid}")
    return Response(content=twiml, media_type="application/xml")


@app.post("/api/call/wait")
async def call_wait(
    request: Request,
    callSid: str,
    CallSid: str = Form(default=""),
    CallStatus: str = Form(default=""),
):
    """
    Polling endpoint called by Twilio every few seconds while caller is on hold.
    Returns hold TwiML until the agent accepts, then returns the SIP bridge TwiML.
    """
    await require_twilio_signature(request)
    # callSid can come from query param (our redirect) or Twilio form POST
    sid = callSid or CallSid
    room_name = f"inbound_{sid}"
    print(f"[Twilio Wait] Polling for callSid={sid} room={room_name} TwilioStatus={CallStatus}")

    call_info = active_calls.get(room_name)
    worker = active_workers.get(room_name)
    worker_started = bool(
        worker_start_states.get(room_name)
        and worker_start_states[room_name].event.is_set()
        and not worker_start_states[room_name].error
    )
    isolation_confirmed = bool(isolation_events.get(room_name) and isolation_events[room_name].is_set())
    if call_info and call_info.get("isolation_failed"):
        return Response(content="""<?xml version="1.0" encoding="UTF-8"?>
<Response><Hangup/></Response>""", media_type="application/xml")
    can_check_boundary = bool(
        call_info
        and call_info.get("status") == "accepted"
        and worker
        and worker_started
        and isolation_confirmed
        and worker.is_ready
    )
    if can_check_boundary:
        try:
            worker = await _ensure_pstn_worker_ready(
                room_name,
                call_info.get("agent_gender", "male"),
            )
        except HTTPException as exc:
            print(
                f"[Twilio Wait] Voice session not ready at bridge boundary "
                f"for {room_name}: {exc.status_code}; holding"
            )
        else:
            _persist_engine_state(call_info, worker)
            sip_domain = get_livekit_sip_domain()
            status_callback = f"{SERVER_URL.rstrip('/') if SERVER_URL else ''}/api/call/status-event"
            print(f"[Twilio Wait] Agent accepted! Bridging to LiveKit SIP: {room_name}@{sip_domain}")
            twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial timeout="60" action="{status_callback}" method="POST">
        <Sip>sip:{room_name}@{sip_domain};transport=tcp</Sip>
    </Dial>
</Response>"""
            return Response(content=twiml, media_type="application/xml")

    if call_info is None:
        # Call was ended/rejected
        print(f"[Twilio Wait] Call {sid} not found — hanging up")
        return Response(content="""<?xml version="1.0" encoding="UTF-8"?>
<Response><Hangup/></Response>""", media_type="application/xml")

    else:
        # Still ringing, or accepted but the voice engine isn't ready yet —
        # play hold and re-poll in 3 seconds
        wait_url = f"{SERVER_URL.rstrip('/') if SERVER_URL else ''}/api/call/wait?callSid={sid}"
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Play loop="1">https://demo.twilio.com/docs/classic.mp3</Play>
    <Redirect method="POST">{wait_url}</Redirect>
</Response>"""
        return Response(content=twiml, media_type="application/xml")

class AcceptCallRequest(BaseModel):
    roomName: str = Field(min_length=1, max_length=128, pattern=r"^inbound_[A-Za-z0-9_.-]+$")
    agentIdentity: str = Field(min_length=5, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    agentGender: Literal["male", "female"] = "male"
    voiceEngine: Literal["rvc"] = "rvc"

@app.post("/api/call/accept", dependencies=[Depends(require_control_token)])
async def call_accept(request: AcceptCallRequest):
    """
    Triggered when an agent accepts an incoming call.
    - Sets call status to 'accepted' so /api/call/wait stops holding and bridges to LiveKit SIP.
    - Returns LiveKit access token for the agent to join the room.
    """
    room_name = request.roomName
    _require_agent_identity(request.agentIdentity)
    print(f"[Call Accept] Agent {request.agentIdentity} accepting room={room_name}")

    if room_name not in active_calls:
        raise HTTPException(status_code=404, detail="Incoming call not found.")
    try:
        await _do_start_bot(
            room_name,
            request.agentGender,
            request.voiceEngine,
            require_real_engine=True,
        )
        await _wait_for_worker_started(room_name)
        worker = await _ensure_pstn_worker_ready(room_name, request.agentGender)
    except Exception as exc:
        await _cleanup_room_state(room_name, delete_room=True)
        raise HTTPException(status_code=503, detail="Unable to start the voice bot.") from exc

    # Mark as 'accepted' — /api/call/wait polls this and will then bridge the SIP call
    active_calls[room_name]["status"] = "accepted"
    active_calls[room_name]["agent_gender"] = request.agentGender
    _persist_engine_state(active_calls[room_name], worker)
    print(f"[Call Accept] Status set to 'accepted' for room={room_name}")

    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(status_code=500, detail="LiveKit server keys are not configured.")

    # Fire-and-forget: unsubscribe the inbound SIP caller from the raw agent mic so
    # they only hear the bot's converted Keira voice. For inbound calls the SIP
    # participant identity is the caller's phone number (From field), but LiveKit
    # names them by their From URI — we scan by exclusion instead of hardcoded identity.
    # The helper retries until the agent browser joins and publishes their mic track.
    async def isolate_inbound_sip():
        isolated = await _restrict_sip_audio(room_name, sip_identity="sip-caller")
        if isolated:
            isolation_events.setdefault(room_name, asyncio.Event()).set()
        elif room_name in active_calls:
            active_calls[room_name]["isolation_failed"] = True
            print(f"[SIP Isolation] Inbound isolation failed closed for room {room_name}")

    _spawn_observed_task(
        isolate_inbound_sip(),
        name=f"keira-sip-isolation-{room_name}",
    )

    # Generate access token for the agent
    agent_token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
        .with_identity(request.agentIdentity) \
        .with_name(f"Agent {request.agentIdentity}") \
        .with_grants(api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=True,
            can_subscribe=True
        )) \
        .with_ttl(datetime.timedelta(seconds=3600))

    return {
        "status": "accepted",
        "roomName": room_name,
        "token": agent_token.to_jwt(),
        "serverUrl": LIVEKIT_URL,
        "requested_engine": worker.requested_engine,
        "effective_engine": worker.effective_engine,
        "fallback_reason": worker.fallback_reason or "",
        "model_version": worker.model_version,
    }

class EndCallRequest(BaseModel):
    roomName: str

async def _do_end_call(room_name: str, reason: str = "") -> None:
    call_info = active_calls.get(room_name)
    # 1. Stop the voice conversion bot and cancel managed lifecycle state.
    await _cleanup_room_state(room_name)

    # 2. Programmatically close the LiveKit Room (disconnects all participants)
    if LIVEKIT_API_KEY and LIVEKIT_API_SECRET:
        try:
            lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
            await lk.aclose()
            print(f"[Server] Programmatically deleted LiveKit room: {room_name}")
        except Exception as e:
            print(f"[Server Error deleting room] {e}")

    # 3. Twilio client hangup fallback (inbound calls have a Twilio CallSid).
    # Outbound calls are LiveKit-originated SIP — there is no Twilio CallSid.
    # Those are ended by deleting the LiveKit room (step 2 above).
    if twilio_client and call_info:
        if call_info.get("direction") == "inbound" and "call_sid" in call_info:
            try:
                call_handle = twilio_client.calls(call_info["call_sid"])
                await asyncio.to_thread(call_handle.update, status="completed")
                print(f"[Server] Twilio CallSid {call_info['call_sid']} hung up programmatically")
            except Exception as e:
                print(f"[Server Error hanging up Twilio call] {e}")

    # 4. Broadcast end call to agents (include optional reason)
    await manager.broadcast({
        "event": "call_ended",
        "roomName": room_name,
        "reason": reason
    })

@app.post("/api/call/end", dependencies=[Depends(require_control_token)])
async def call_end(request: EndCallRequest):
    """
    Disconnects the bot, deletes the LiveKit room, and broadcasts the disconnect event.
    """
    await _do_end_call(request.roomName)
    return {"status": "ended", "roomName": request.roomName}

@app.get("/api/call/status", dependencies=[Depends(require_control_token)])
async def call_status(roomName: str = None):
    """
    Returns status of active calls.
    """
    if roomName:
        if roomName in active_calls:
            return active_calls[roomName]
        raise HTTPException(status_code=404, detail="Call not found")
    return {"active_calls": list(active_calls.values())}

# --- TWILIO + LIVEKIT SETUP & HEALTH ENDPOINTS ---

@app.get("/api/health")
async def health_check():
    """
    Returns the configuration and live status of all integrations.
    Check this after deployment to confirm everything is wired up correctly.
    """
    return {
        "livekit": {
            "url": LIVEKIT_URL,
            "api_key_set": bool(LIVEKIT_API_KEY),
            "api_secret_set": bool(LIVEKIT_API_SECRET),
        },
        "twilio": {
            "account_sid_set": bool(TWILIO_ACCOUNT_SID),
            "auth_token_set": bool(TWILIO_AUTH_TOKEN),
            "phone_number_configured": bool(TWILIO_PHONE_NUMBER),
            "sip_uri_configured": bool(TWILIO_SIP_URI),
            "client_initialized": bool(twilio_client),
        },
        "livekit_sip": {
            "outbound_trunk_id": TWILIO_SIP_TRUNK_ID or "not set — run POST /api/setup",
            "inbound_webhook": f"{SERVER_URL}/api/call/inbound" if SERVER_URL else "SERVER_URL not set",
            "sip_domain": get_livekit_sip_domain(),
        },
        "rvc": {
            "endpoint": RVC_ENDPOINT_URL or "not configured (PSTN calls fail closed)",
        },
        "server_url": SERVER_URL or "not set",
        "active_calls": len(active_calls),
        "active_bots": len(active_workers),
    }

@app.post("/api/setup", dependencies=[Depends(require_control_token)])
async def setup_integrations():
    """
    One-time setup endpoint. Call this once after filling in all credentials.

    It will:
      1. Create a LiveKit SIP Outbound Trunk connecting LiveKit → Twilio → PSTN
      2. Configure your Twilio phone number's inbound webhook to point to this server

    After running this, copy the returned trunk_id and save it as TWILIO_SIP_TRUNK_ID
    in your Render dashboard, then redeploy.
    """
    import traceback as _tb

    results = {}
    errors = {}

    # Step 1: Create LiveKit SIP Outbound Trunk
    if not TWILIO_SIP_URI:
        errors["livekit_sip_trunk"] = "TWILIO_SIP_URI is not set. Set it to your Twilio Elastic SIP Trunk termination URI (e.g. myproject.pstn.twilio.com)."
    elif not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER]):
        errors["livekit_sip_trunk"] = "Missing TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, or TWILIO_PHONE_NUMBER."
    elif not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        errors["livekit_sip_trunk"] = "Missing LIVEKIT_API_KEY or LIVEKIT_API_SECRET."
    else:
        lk = None
        try:
            lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)

            # Check if trunk already exists to avoid recreating it on every /api/setup call.
            # Recreating the trunk changes its ID, which breaks the TWILIO_SIP_TRUNK_ID env var
            # and causes a 404 "object cannot be found" on the next outbound call.
            existing = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
            existing_trunk = next((t for t in existing.items if t.name == "Keira Twilio Outbound"), None)

            clean_phone = TWILIO_PHONE_NUMBER.replace(" ", "")

            if existing_trunk:
                # Reuse the existing trunk — its ID is stable and the env var stays valid.
                trunk_id = existing_trunk.sip_trunk_id
                print(f"[Setup] Reusing existing LiveKit SIP Outbound Trunk: {trunk_id}")
                results["livekit_sip_trunk"] = {
                    "status": "already_exists",
                    "trunk_id": trunk_id,
                    "address": existing_trunk.address,
                    "numbers": list(existing_trunk.numbers),
                    "note": "Trunk already exists and was reused. TWILIO_SIP_TRUNK_ID env var remains valid.",
                }
            else:
                trunk_info = api.SIPOutboundTrunkInfo(
                    name="Keira Twilio Outbound",
                    address=TWILIO_SIP_URI,
                    numbers=[clean_phone],
                    auth_username=TWILIO_SIP_USERNAME,
                    auth_password=TWILIO_SIP_PASSWORD,
                )
                created = await lk.sip.create_outbound_trunk(
                    api.CreateSIPOutboundTrunkRequest(trunk=trunk_info)
                )
                trunk_id = created.sip_trunk_id
                print(f"[Setup] Created LiveKit SIP Outbound Trunk: {trunk_id} with number {clean_phone}")
                results["livekit_sip_trunk"] = {
                    "status": "created",
                    "trunk_id": trunk_id,
                    "address": TWILIO_SIP_URI,
                    "numbers": [clean_phone],
                    "action": f"Save TWILIO_SIP_TRUNK_ID={trunk_id} in your Render env vars then redeploy",
                }
        except Exception as e:
            _tb.print_exc()
            errors["livekit_sip_trunk"] = str(e)
        finally:
            if lk:
                await lk.aclose()

    # Step 2: Configure Twilio phone number inbound webhook
    if not SERVER_URL:
        errors["twilio_webhook"] = "SERVER_URL is not set. Set it to your public server URL (e.g. https://keira-backend.onrender.com)."
    elif not twilio_client:
        errors["twilio_webhook"] = "Twilio client not initialized — check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN."
    elif not TWILIO_PHONE_NUMBER:
        errors["twilio_webhook"] = "TWILIO_PHONE_NUMBER is not set."
    else:
        try:
            webhook_url = f"{SERVER_URL.rstrip('/')}/api/call/inbound"
            numbers = twilio_client.incoming_phone_numbers.list(phone_number=TWILIO_PHONE_NUMBER)
            if not numbers:
                errors["twilio_webhook"] = f"Phone number {TWILIO_PHONE_NUMBER} not found in your Twilio account."
            else:
                numbers[0].update(voice_url=webhook_url, voice_method="POST")
                print(f"[Setup] Twilio webhook configured: {webhook_url}")
                results["twilio_webhook"] = {
                    "status": "configured",
                    "phone_number": TWILIO_PHONE_NUMBER,
                    "webhook_url": webhook_url,
                }
        except Exception as e:
            _tb.print_exc()
            errors["twilio_webhook"] = str(e)

    # Steps 3+4: Reconcile only Keira-owned resources. Never delete unrelated
    # trunks/rules from a shared LiveKit project.
    inbound_trunk_id = None
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        errors["sip_inbound_trunk"] = "Missing LIVEKIT_API_KEY or LIVEKIT_API_SECRET."
        errors["sip_dispatch_rule"] = "Missing LIVEKIT_API_KEY or LIVEKIT_API_SECRET."
    else:
        lk = None
        try:
            lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)

            # --- Inbound trunk ---
            existing_trunks = await lk.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
            keira_trunk = None
            ignored_trunks = []
            for t in existing_trunks.items:
                if t.name == "Keira Twilio Inbound":
                    keira_trunk = t
                else:
                    ignored_trunks.append({"trunk_id": t.sip_trunk_id, "name": t.name})

            if keira_trunk is None:
                keira_trunk = await lk.sip.create_inbound_trunk(
                    api.CreateSIPInboundTrunkRequest(
                        trunk=api.SIPInboundTrunkInfo(
                            name="Keira Twilio Inbound",
                            # No auth or IP restrictions — Twilio Programmable Voice
                            # sends SIP without pre-shared credentials.
                        )
                    )
                )
                print(f"[Setup] Created SIP inbound trunk: {keira_trunk.sip_trunk_id}")

            inbound_trunk_id = keira_trunk.sip_trunk_id
            results["sip_inbound_trunk"] = {
                "status": "ready",
                "trunk_id": inbound_trunk_id,
                "ignored_unrelated": ignored_trunks,
            }

            # --- Dispatch rule ---
            existing_rules = await lk.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
            keira_rule = next(
                (r for r in existing_rules.items if r.name == "Keira Inbound Routing"),
                None,
            )
            if keira_rule:
                configured_trunks = set(getattr(keira_rule, "trunk_ids", []))
                if configured_trunks and inbound_trunk_id not in configured_trunks:
                    errors["sip_dispatch_rule"] = (
                        "Keira Inbound Routing exists but targets a different trunk; "
                        "no rules were deleted. Reconcile it manually."
                    )
                else:
                    results["sip_dispatch_rule"] = {
                        "status": "already_exists",
                        "rule_id": keira_rule.sip_dispatch_rule_id,
                        "trunk_id": inbound_trunk_id,
                        "ignored_unrelated": len(existing_rules.items) - 1,
                    }
            else:
                rule = await lk.sip.create_dispatch_rule(
                    api.CreateSIPDispatchRuleRequest(
                        rule=api.SIPDispatchRule(
                            dispatch_rule_callee=api.SIPDispatchRuleCallee(
                                room_prefix="",
                                randomize=False,
                            )
                        ),
                        trunk_ids=[inbound_trunk_id],
                        name="Keira Inbound Routing",
                    )
                )
                print(f"[Setup] Created SIP dispatch rule: {rule.sip_dispatch_rule_id}")
                results["sip_dispatch_rule"] = {
                    "status": "created",
                    "rule_id": rule.sip_dispatch_rule_id,
                    "trunk_id": inbound_trunk_id,
                    "ignored_unrelated": len(existing_rules.items),
                }

        except Exception as e:
            _tb.print_exc()
            errors["sip_setup"] = str(e)
        finally:
            if lk:
                await lk.aclose()

    overall_ok = not errors
    return {
        "ok": overall_ok,
        "results": results,
        "errors": errors,
    }

# --- GPU KEEP-WARM ENDPOINT ---

@app.post("/api/warmup", dependencies=[Depends(require_control_token)])
async def warmup_gpu():
    """
    Pings the RVC endpoint /health to ensure the Modal GPU is warm.
    Retries every 30 seconds for up to 6 minutes to handle Modal cold starts.
    """
    if not RVC_ENDPOINT_URL:
        print("[Server Warmup] FAILED: No RVC_ENDPOINT_URL configured in .env")
        return {"status": "skipped", "message": "No RVC endpoint URL configured."}

    return await _wait_for_rvc_ready(max_wait_seconds=360.0, poll_interval=30.0)

# --- GPU DEPLOY ENDPOINT ---

@app.post("/api/deploy", dependencies=[Depends(require_control_token)])
async def deploy_gpu():
    """
    Executes 'modal deploy modal_deploy/worker.py' to deploy the RVC worker to Modal.
    Requires MODAL_TOKEN_ID and MODAL_TOKEN_SECRET to be configured in Render's environment variables.
    """
    import subprocess
    import os
    
    # Check if Modal tokens are present
    token_id = os.getenv("MODAL_TOKEN_ID")
    token_secret = os.getenv("MODAL_TOKEN_SECRET")
    
    if not token_id or not token_secret:
        return {
            "status": "error",
            "message": "MODAL_TOKEN_ID or MODAL_TOKEN_SECRET environment variables are missing on Render."
        }
        
    print("[Server Deploy] Triggering modal deploy modal_deploy/worker.py...")
    try:
        def run_deploy():
            # Build env variables with path overrides to find the modal executable
            env = os.environ.copy()
            # Try to run modal deploy
            res = subprocess.run(
                ["modal", "deploy", "modal_deploy/worker.py"],
                capture_output=True,
                text=True,
                env=env,
                cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            )
            return res
            
        res = await asyncio.to_thread(run_deploy)
        print(f"[Server Deploy] STDOUT: {res.stdout}")
        print(f"[Server Deploy] STDERR: {res.stderr}")
        
        if res.returncode == 0:
            return {
                "status": "success",
                "message": "Modal GPU deployed successfully!",
                "output": res.stdout
            }
        else:
            return {
                "status": "error",
                "message": f"Deployment failed: {res.stderr or res.stdout}",
                "output": res.stdout
            }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            "status": "error",
            "message": f"Deployment exception: {str(e)}"
        }

# --- WEBSOCKET SIGNALING ROUTE ---


@app.websocket("/api/call/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Browser WebSocket clients cannot set arbitrary Authorization headers. Use a
    # subprotocol for the in-memory control token instead of putting it in the URL,
    # where access logs and proxy metrics commonly record query strings.
    offered = [item.strip() for item in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    auth_protocol = next((item for item in offered if item.startswith("keira-auth.")), "")
    token = auth_protocol[len("keira-auth."):] if auth_protocol else ""
    if not CONTROL_PLANE_TOKEN or not verify_bearer_token(f"Bearer {token}", CONTROL_PLANE_TOKEN):
        await websocket.close(code=1008, reason="Authentication required")
        return
    await websocket.accept(subprotocol=auth_protocol)
    await manager.connect(websocket, already_accepted=True)
    try:
        while True:
            # Maintain active connection and listen for client-side pings
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"event": "pong"})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)

# --- FRONTEND ROUTING ---

# Mount static frontend files if directory exists
frontend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
else:
    @app.get("/")
    async def fallback_root():
        return {"message": "Keira MVP Server Running. Frontend static files directory is missing."}
