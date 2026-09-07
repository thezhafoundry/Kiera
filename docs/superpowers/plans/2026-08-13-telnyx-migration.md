# Twilio → Telnyx PSTN Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Twilio as Keira's PSTN/telephony provider with Telnyx, keeping LiveKit (room/media/SIP trunk) and the entire RVC/Modal voice pipeline untouched. Only `backend/main.py`'s telephony integration layer changes.

**Architecture:** Twilio's model is TwiML request/response (Twilio POSTs, you return XML synchronously, `/api/call/wait` polls every 3s). Telnyx's Call Control model is webhook-event-driven (Telnyx POSTs a JSON event, you push back async commands like `answer`/`transfer` via `POST /v2/calls/{id}/actions/...`, and Telnyx tells you what happened next via more webhooks). This is **not a syntax swap** — `call_inbound`/`call_wait`'s polling state machine is replaced by a webhook-event handler that reacts to `call.initiated` → `call.answered` and issues a `transfer` command once the agent is ready and the LiveKit SIP bridge is confirmed. The existing `active_calls` dict, `isolation_events`, and `_restrict_sip_audio` machinery stay exactly as they are — only what drives them changes.

**Tech Stack:** FastAPI (existing), `telnyx` Python SDK (new dependency, replaces `twilio` SDK), Ed25519 signature verification (`cryptography` package, already a transitive dep of most crypto stacks — confirm/add explicitly), LiveKit SIP trunk API (unchanged usage, new address/credentials).

## Global Constraints

- **No live traffic exists today** — this migration can be built, tested, and cut over without a maintenance window or parallel-run safety net. Still: every live account mutation (creating Telnyx connections, changing Render env vars, running the new `/api/setup`) requires explicit user confirmation before executing, per standing project policy — do not auto-run these.
- **Never reintroduce the raw-audio-on-failure fallback.** This migration touches none of `pipeline.py`'s fail-closed behavior; if any task's implementation appears to need touching `pipeline.py` or the converters, stop — that's out of scope and a sign of scope creep.
- **`_restrict_sip_audio`, `active_calls`, `isolation_events`, `_do_start_bot`, `_ensure_pstn_worker_ready` are unchanged.** They operate purely on LiveKit room/participant state and don't know or care which SIP provider is upstream of the SIP participant.
- **Two facts flagged by research as unverified against a live source — confirm before Task 2/6 lock in code that depends on them:**
  1. The exact call signature of the `telnyx` Python SDK's webhook-verification helper (`client.webhooks.unwrap(body, headers)` per docs search — verify against the actually-installed SDK version).
  2. Whether "Singapore" (or an APAC-appropriate low-latency option) is an available Telnyx AnchorSite/SIP Region today — check the Telnyx Mission Control Portal directly; do not guess a region string into `.env`.
- **Keep the Twilio code path removable, not deleted mid-plan.** Until Task 7 (cutover), both providers' code coexist behind an env-selected branch so `main` never has a broken telephony layer.

---

## File Structure

| File | Change |
|---|---|
| `backend/telnyx_integration.py` | **New.** All Telnyx-specific logic: signature verification, webhook event parsing, Call Control command helpers (`answer_call`, `transfer_call`, `start_recording`, `originate_call`). Mirrors the shape of the Twilio-specific code currently inlined in `main.py`, but isolated so it can be unit-tested without FastAPI request plumbing. |
| `backend/main.py` | Modified. New Telnyx webhook routes (`/api/telnyx/webhook`) alongside (not replacing, until Task 7) the existing Twilio routes. `_find_outbound_trunk`, `/api/setup`, `/api/health` gain a provider-aware branch. `_do_end_call`'s Twilio hangup fallback gains a Telnyx equivalent. |
| `backend/test_telnyx_integration.py` | **New.** Unit tests for signature verification and webhook payload parsing — no live Telnyx account needed (uses recorded/synthetic payloads and a locally generated Ed25519 keypair). |
| `backend/requirements.txt` | Add `telnyx`, remove `twilio` in the final cutover task only. |
| `.env` / `README.md` §3 | New env vars documented: `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`, `TELNYX_CONNECTION_ID`, `TELNYX_PHONE_NUMBER`, `TELNYX_SIP_USERNAME`, `TELNYX_SIP_PASSWORD`, `TELNYX_SIP_TRUNK_ID` (LiveKit-side, same role as today's `TWILIO_SIP_TRUNK_ID`). |
| `CLAUDE.md` | Env var reference line updated once cutover (Task 7) lands. |

---

### Task 1: Telnyx webhook signature verification

**Files:**
- Create: `backend/telnyx_integration.py`
- Test: `backend/test_telnyx_integration.py`

**Interfaces:**
- Produces: `verify_telnyx_signature(raw_body: bytes, signature_header: str, timestamp_header: str, public_key: str) -> bool` — pure function, no FastAPI dependency, used by Task 3's route dependency.

- [ ] **Step 1: Confirm the SDK helper signature against the installed package**

Before writing anything, resolve the two research caveats:
```bash
pip index versions telnyx
pip download telnyx --no-deps -d /tmp/telnyx_check
python -c "import zipfile,glob; z=zipfile.ZipFile(glob.glob('/tmp/telnyx_check/*.whl')[0]); print([n for n in z.namelist() if 'webhook' in n.lower()])"
```
Read the matched file's source directly (unzip and open it) to confirm the exact function name, parameter names, and whether it takes raw bytes or a parsed dict. If the SDK helper shape differs from `client.webhooks.unwrap(body, headers)`, adjust Step 3 below to match reality — do not proceed on the assumption alone.

- [ ] **Step 2: Write the failing test using a synthetic Ed25519 keypair (no live Telnyx account needed)**

```python
# backend/test_telnyx_integration.py
import base64
import time
import pytest
from nacl.signing import SigningKey  # PyNaCl — add to requirements.txt if not already present
from backend.telnyx_integration import verify_telnyx_signature


def _make_signed_payload(signing_key: SigningKey, body: bytes, timestamp: str):
    message = f"{timestamp}|".encode() + body
    signature = signing_key.sign(message).signature
    return base64.b64encode(signature).decode()


def test_verify_telnyx_signature_accepts_valid_signature():
    signing_key = SigningKey.generate()
    public_key_b64 = base64.b64encode(bytes(signing_key.verify_key)).decode()
    body = b'{"data":{"event_type":"call.initiated"}}'
    timestamp = str(int(time.time()))
    signature = _make_signed_payload(signing_key, body, timestamp)

    assert verify_telnyx_signature(body, signature, timestamp, public_key_b64) is True


def test_verify_telnyx_signature_rejects_tampered_body():
    signing_key = SigningKey.generate()
    public_key_b64 = base64.b64encode(bytes(signing_key.verify_key)).decode()
    body = b'{"data":{"event_type":"call.initiated"}}'
    timestamp = str(int(time.time()))
    signature = _make_signed_payload(signing_key, body, timestamp)

    tampered_body = b'{"data":{"event_type":"call.hangup"}}'
    assert verify_telnyx_signature(tampered_body, signature, timestamp, public_key_b64) is False


def test_verify_telnyx_signature_rejects_wrong_key():
    signing_key = SigningKey.generate()
    other_key_public = base64.b64encode(bytes(SigningKey.generate().verify_key)).decode()
    body = b'{"data":{"event_type":"call.initiated"}}'
    timestamp = str(int(time.time()))
    signature = _make_signed_payload(signing_key, body, timestamp)

    assert verify_telnyx_signature(body, signature, timestamp, other_key_public) is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest backend/test_telnyx_integration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backend.telnyx_integration'`

- [ ] **Step 4: Write minimal implementation**

```python
# backend/telnyx_integration.py
import base64

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey


def verify_telnyx_signature(
    raw_body: bytes,
    signature_header: str,
    timestamp_header: str,
    public_key_b64: str,
) -> bool:
    """Verify a Telnyx webhook's Ed25519 signature against the account public key.

    Signing string is "{timestamp}|{raw_body}" per Telnyx's webhook spec —
    must be verified against the RAW body bytes, not a re-serialized/parsed copy.
    """
    try:
        verify_key = VerifyKey(base64.b64decode(public_key_b64))
        signature = base64.b64decode(signature_header)
        message = f"{timestamp_header}|".encode() + raw_body
        verify_key.verify(message, signature)
        return True
    except (BadSignatureError, ValueError):
        return False
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest backend/test_telnyx_integration.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Add `pynacl` to requirements**

Add `pynacl` to `backend/requirements.txt` (alphabetical position, matching existing style).

- [ ] **Step 7: Commit**

```bash
git add backend/telnyx_integration.py backend/test_telnyx_integration.py backend/requirements.txt
git commit -m "feat(telnyx): add Ed25519 webhook signature verification"
```

---

### Task 2: Telnyx webhook payload parsing + Call Control command helpers

**Files:**
- Modify: `backend/telnyx_integration.py`
- Test: `backend/test_telnyx_integration.py`

**Interfaces:**
- Consumes: nothing new from Task 1.
- Produces:
  - `parse_telnyx_event(payload: dict) -> TelnyxCallEvent` (dataclass: `event_type: str`, `call_control_id: str`, `call_leg_id: str`, `from_number: str`, `to_number: str`, `raw: dict`)
  - `async def answer_call(client, call_control_id: str) -> None`
  - `async def transfer_call(client, call_control_id: str, sip_uri: str, transport: str = "TCP") -> None`
  - `async def start_dual_recording(client, call_control_id: str) -> None`
  - `async def hangup_call(client, call_control_id: str) -> None`
  - `async def originate_call(client, connection_id: str, to_number: str, from_number: str, webhook_url: str) -> str` (returns the new call's `call_control_id`)

  All command helpers take a `client` (the `telnyx` SDK client instance) as their first arg rather than constructing one internally — keeps them testable with a fake/mock client, matches the existing codebase's pattern of injecting the LiveKit `api.LiveKitAPI` instance rather than having helpers construct their own.

- [ ] **Step 1: Write the failing test for payload parsing**

```python
def test_parse_telnyx_event_extracts_call_fields():
    from backend.telnyx_integration import parse_telnyx_event

    payload = {
        "data": {
            "event_type": "call.initiated",
            "id": "evt_123",
            "occurred_at": "2026-08-13T10:00:00Z",
            "payload": {
                "call_control_id": "v3:abc123",
                "call_leg_id": "leg_abc",
                "from": "+15551234567",
                "to": "+15557654321",
            },
        }
    }

    event = parse_telnyx_event(payload)

    assert event.event_type == "call.initiated"
    assert event.call_control_id == "v3:abc123"
    assert event.call_leg_id == "leg_abc"
    assert event.from_number == "+15551234567"
    assert event.to_number == "+15557654321"
    assert event.raw == payload
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest backend/test_telnyx_integration.py::test_parse_telnyx_event_extracts_call_fields -v`
Expected: FAIL with `ImportError: cannot import name 'parse_telnyx_event'`

- [ ] **Step 3: Write minimal implementation for parsing**

```python
# backend/telnyx_integration.py (append)
from dataclasses import dataclass


@dataclass(frozen=True)
class TelnyxCallEvent:
    event_type: str
    call_control_id: str
    call_leg_id: str
    from_number: str
    to_number: str
    raw: dict


def parse_telnyx_event(payload: dict) -> TelnyxCallEvent:
    data = payload["data"]
    call_payload = data.get("payload", {})
    return TelnyxCallEvent(
        event_type=data["event_type"],
        call_control_id=call_payload.get("call_control_id", ""),
        call_leg_id=call_payload.get("call_leg_id", ""),
        from_number=call_payload.get("from", ""),
        to_number=call_payload.get("to", ""),
        raw=payload,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest backend/test_telnyx_integration.py::test_parse_telnyx_event_extracts_call_fields -v`
Expected: PASS

- [ ] **Step 5: Write failing tests for the Call Control command helpers using a fake client**

```python
class _FakeCallsResource:
    def __init__(self):
        self.calls = []  # list of (call_control_id, action, kwargs)

    async def actions_answer(self, call_control_id, **kwargs):
        self.calls.append((call_control_id, "answer", kwargs))

    async def actions_transfer(self, call_control_id, **kwargs):
        self.calls.append((call_control_id, "transfer", kwargs))

    async def actions_record_start(self, call_control_id, **kwargs):
        self.calls.append((call_control_id, "record_start", kwargs))

    async def actions_hangup(self, call_control_id, **kwargs):
        self.calls.append((call_control_id, "hangup", kwargs))

    async def dial(self, **kwargs):
        self.calls.append((None, "dial", kwargs))
        return type("Result", (), {"data": type("Data", (), {"call_control_id": "v3:new_call"})()})()


class _FakeClient:
    def __init__(self):
        self.calls = _FakeCallsResource()


@pytest.mark.asyncio
async def test_answer_call_calls_actions_answer():
    from backend.telnyx_integration import answer_call

    client = _FakeClient()
    await answer_call(client, "v3:abc123")

    assert client.calls.calls == [("v3:abc123", "answer", {})]


@pytest.mark.asyncio
async def test_transfer_call_passes_sip_uri_and_transport():
    from backend.telnyx_integration import transfer_call

    client = _FakeClient()
    await transfer_call(client, "v3:abc123", "sip:room@xxx.sip.livekit.cloud", transport="TCP")

    assert client.calls.calls == [
        ("v3:abc123", "transfer", {"to": "sip:room@xxx.sip.livekit.cloud", "sip_transport_protocol": "TCP"})
    ]


@pytest.mark.asyncio
async def test_start_dual_recording_requests_dual_channel_wav():
    from backend.telnyx_integration import start_dual_recording

    client = _FakeClient()
    await start_dual_recording(client, "v3:abc123")

    assert client.calls.calls == [("v3:abc123", "record_start", {"channels": "dual", "format": "wav"})]


@pytest.mark.asyncio
async def test_originate_call_returns_new_call_control_id():
    from backend.telnyx_integration import originate_call

    client = _FakeClient()
    call_control_id = await originate_call(
        client,
        connection_id="conn_123",
        to_number="+15557654321",
        from_number="+15551234567",
        webhook_url="https://example.com/api/telnyx/webhook",
    )

    assert call_control_id == "v3:new_call"
    assert client.calls.calls == [
        (None, "dial", {
            "connection_id": "conn_123",
            "to": "+15557654321",
            "from_": "+15551234567",
            "webhook_url": "https://example.com/api/telnyx/webhook",
        })
    ]
```

Note: `pytest-asyncio` must be installed and configured (`asyncio_mode = auto` or the `@pytest.mark.asyncio` decorator as shown) — check `backend/requirements.txt`/`pytest.ini`/`pyproject.toml` for existing async test config before assuming; the existing `test_pipeline.py` already drives async code, so this is likely already set up. If not, add `pytest-asyncio` and the minimal config.

- [ ] **Step 6: Run tests to verify they fail**

Run: `python -m pytest backend/test_telnyx_integration.py -v`
Expected: 4 new FAILs with `ImportError`

- [ ] **Step 7: Write minimal implementation for the command helpers**

```python
# backend/telnyx_integration.py (append)

async def answer_call(client, call_control_id: str) -> None:
    await client.calls.actions_answer(call_control_id)


async def transfer_call(client, call_control_id: str, sip_uri: str, transport: str = "TCP") -> None:
    await client.calls.actions_transfer(
        call_control_id,
        to=sip_uri,
        sip_transport_protocol=transport,
    )


async def start_dual_recording(client, call_control_id: str) -> None:
    await client.calls.actions_record_start(
        call_control_id,
        channels="dual",
        format="wav",
    )


async def hangup_call(client, call_control_id: str) -> None:
    await client.calls.actions_hangup(call_control_id)


async def originate_call(
    client,
    connection_id: str,
    to_number: str,
    from_number: str,
    webhook_url: str,
) -> str:
    result = await client.calls.dial(
        connection_id=connection_id,
        to=to_number,
        from_=from_number,
        webhook_url=webhook_url,
    )
    return result.data.call_control_id
```

**Before trusting this shape as final**, verify the actual `telnyx` Python SDK's method names (`client.calls.actions_answer` vs. some other name — the SDK may nest differently, e.g. `client.Call(call_control_id).answer()` in an older-generation SDK style) against the installed package the same way Task 1 Step 1 did. Telnyx has both a legacy synchronous SDK (`telnyx.Call`) and a newer typed async client — confirm which one the pinned `requirements.txt` version provides, and adjust the helper bodies (not the test-visible function signatures) to match. If the SDK is synchronous-only, wrap calls in `asyncio.to_thread(...)` rather than blocking the event loop — same pattern the codebase already uses for other blocking SDK calls (see `CLAUDE.md`'s "never block the event loop" rule).

- [ ] **Step 8: Run tests to verify they pass**

Run: `python -m pytest backend/test_telnyx_integration.py -v`
Expected: PASS (8 tests total)

- [ ] **Step 9: Commit**

```bash
git add backend/telnyx_integration.py backend/test_telnyx_integration.py backend/requirements.txt
git commit -m "feat(telnyx): add webhook event parsing and Call Control command helpers"
```

---

### Task 3: Telnyx account setup (LIVE — requires user confirmation before each sub-step)

**Files:** None (external account configuration only). This task produces the values Task 4/5 need as env vars.

**Interfaces:**
- Produces: `TELNYX_API_KEY`, `TELNYX_PUBLIC_KEY`, `TELNYX_CONNECTION_ID`, `TELNYX_PHONE_NUMBER`, `TELNYX_SIP_USERNAME`, `TELNYX_SIP_PASSWORD` — recorded in a local `.env` (never committed) for Task 5's manual verification, and later in Render env vars at cutover (Task 7).

- [ ] **Step 1: Stop and confirm with the user before touching the Telnyx account**

This is a live external account mutation. Confirm explicitly: purchase/assign a Telnyx phone number, create a Call Control Application (gives a `connection_id` + lets you set the webhook URL), and create the Credentials-type SIP Connection (matches the screenshot already in progress) for LiveKit's outbound leg. Do not proceed to Steps 2-4 without that confirmation.

- [ ] **Step 2: Create a Telnyx Call Control Application**

In the Telnyx portal: Voice → Call Control Applications → create one, set its webhook URL to `<SERVER_URL>/api/telnyx/webhook` (placeholder until `SERVER_URL` exists for local dev — use a tunnel like `ngrok`/Cloudflare Tunnel for local webhook testing, same as would be needed for Twilio locally). Record the Application's `connection_id`.

- [ ] **Step 3: Finish the SIP Connection (Credentials type) already open in the portal**

Per the research: LiveKit's outbound leg targets host `sip.telnyx.com` with the credential username/password from this connection. Also set `headers_to_attributes: {"X-Telnyx-Username": "<username>"}` on the eventual LiveKit `SIPOutboundTrunkInfo` (Task 5) — required because `sip.telnyx.com` is a shared hostname across all Telnyx customers and needs the username disambiguated via a SIP header, per LiveKit's own Telnyx integration doc. Record the SIP username/password.

- [ ] **Step 4: Verify the AnchorSite/SIP Region question flagged by research**

Check the Mission Control Portal's connection settings for an AnchorSite or SIP Region dropdown. Confirm whether a Singapore/APAC option exists (Render is in Singapore — see `.agents/context/subsystem-notes.md`'s Modal region notes for why this matters: the Twilio migration hit a real transpacific-routing bug from getting this wrong, see the "Twilio media edge pinning" section). Record whatever is found — this becomes a required input to Task 5, not an assumption.

- [ ] **Step 5: Purchase/port a Telnyx phone number**

Assign it to the Call Control Application from Step 2 so inbound calls route to the right webhook.

No commit — this task produces account state and a short written note (not a repo file) of what was created, to hand into Task 5.

---

### Task 4: Provider-selectable webhook route + inbound call state machine

**Files:**
- Modify: `backend/main.py`

**Interfaces:**
- Consumes: `verify_telnyx_signature`, `parse_telnyx_event`, `TelnyxCallEvent`, `answer_call`, `transfer_call`, `start_dual_recording` from Task 1/2.
- Produces: `POST /api/telnyx/webhook` route. Writes to the same `active_calls`, `isolation_events` dicts the Twilio path already uses, so `_restrict_sip_audio`/`_ensure_pstn_worker_ready`/`_do_start_bot` need zero changes.

- [ ] **Step 1: Add Telnyx client init and env vars alongside the existing Twilio block**

In `backend/main.py`, near the existing `# Twilio Configuration` block (around line 241), add:

```python
# Telnyx Configuration
TELNYX_API_KEY = os.getenv("TELNYX_API_KEY")
TELNYX_PUBLIC_KEY = os.getenv("TELNYX_PUBLIC_KEY")
TELNYX_CONNECTION_ID = os.getenv("TELNYX_CONNECTION_ID")
TELNYX_PHONE_NUMBER = os.getenv("TELNYX_PHONE_NUMBER")
TELNYX_SIP_USERNAME = os.getenv("TELNYX_SIP_USERNAME", "Keira")
TELNYX_SIP_PASSWORD = os.getenv("TELNYX_SIP_PASSWORD", "")
TELNYX_SIP_TRUNK_ID = os.getenv("TELNYX_SIP_TRUNK_ID")

telnyx_client = None
if TELNYX_API_KEY:
    try:
        import telnyx as telnyx_sdk
        telnyx_client = telnyx_sdk.AsyncTelnyx(api_key=TELNYX_API_KEY)  # confirm actual client class name against Task 2 Step 7's SDK check
    except Exception as e:
        print(f"[Server Warning] Failed to initialize Telnyx client: {e}")

# Which provider is authoritative right now. Both code paths coexist until
# cutover (Task 7); this flag exists so /api/setup, /api/health, and
# _find_outbound_trunk can branch without duplicating every endpoint.
TELEPHONY_PROVIDER = os.getenv("TELEPHONY_PROVIDER", "twilio")  # "twilio" | "telnyx"
```

- [ ] **Step 2: Add the signature-verification dependency**

Near `require_twilio_signature` (line 102):

```python
async def require_telnyx_signature(request: Request) -> dict:
    """Validate Telnyx's Ed25519-signed webhook and return the parsed JSON body."""
    if not TELNYX_PUBLIC_KEY:
        raise HTTPException(status_code=503, detail="Telnyx callback validation is not configured.")
    signature = request.headers.get("telnyx-signature-ed25519", "")
    timestamp = request.headers.get("telnyx-timestamp", "")
    if not signature or not timestamp:
        raise HTTPException(status_code=403, detail="Missing Telnyx signature headers.")

    raw_body = await request.body()
    from .telnyx_integration import verify_telnyx_signature
    if not verify_telnyx_signature(raw_body, signature, timestamp, TELNYX_PUBLIC_KEY):
        raise HTTPException(status_code=403, detail="Invalid Telnyx callback signature.")

    import json
    return json.loads(raw_body)
```

- [ ] **Step 3: Add the webhook route implementing the event-driven inbound flow**

Add near the existing `# --- TWILIO & SIP INTEGRATION ENDPOINTS ---` section:

```python
@app.post("/api/telnyx/webhook")
async def telnyx_webhook(payload: dict = Depends(require_telnyx_signature)):
    """
    Telnyx Call Control webhook — event-driven, replaces Twilio's TwiML polling.

    Flow:
      call.initiated  -> answer the call, mark active_calls[room] "ringing",
                          broadcast incoming_call to agent UI (same as Twilio path).
      call.answered   -> no-op here; waiting for the operator to call /api/call/accept,
                          which is unchanged from the Twilio flow.
      call.hangup     -> clean up room state (mirrors _do_end_call's Twilio branch).

    The bridge-to-LiveKit step (equivalent to Twilio's /api/call/wait returning
    <Dial><Sip>) happens in _ensure_pstn_worker_ready's caller once the agent accepts —
    see the transfer_call() invocation added to call_accept in this same task.
    """
    from .telnyx_integration import parse_telnyx_event
    event = parse_telnyx_event(payload)
    print(f"[Telnyx Webhook] {event.event_type} call_control_id={event.call_control_id} from={redact_phone_number(event.from_number)}")

    if event.event_type == "call.initiated":
        # Inbound calls have a "direction" field under payload; outbound calls
        # we originated ourselves also fire call.initiated — only auto-answer
        # calls we didn't originate (direction == "incoming").
        direction = event.raw["data"]["payload"].get("direction", "")
        if direction != "incoming":
            return Response(status_code=204)

        room_name = f"inbound_{event.call_control_id.replace(':', '_')}"
        isolation_events[room_name] = asyncio.Event()
        active_calls[room_name] = {
            "room_name": room_name,
            "from": event.from_number,
            "to": event.to_number or TELNYX_PHONE_NUMBER or "Keira Inbound",
            "status": "ringing",
            "direction": "inbound",
            "call_control_id": event.call_control_id,
            "created_at": datetime.datetime.now().isoformat(),
        }
        from .telnyx_integration import answer_call
        await answer_call(telnyx_client, event.call_control_id)
        await manager.broadcast({
            "event": "incoming_call",
            "roomName": room_name,
            "callerId": event.from_number,
            "callSid": event.call_control_id,
        })
        print(f"[Telnyx Webhook] Answered and broadcasted incoming_call for room={room_name}")

    elif event.event_type == "call.hangup":
        room_name = next(
            (r for r, info in active_calls.items() if info.get("call_control_id") == event.call_control_id),
            None,
        )
        if room_name:
            await _do_end_call(room_name, reason="caller_hangup")

    return Response(status_code=204)
```

- [ ] **Step 4: Wire the bridge-to-LiveKit transfer into `call_accept`**

Modify `call_accept` (existing function, line ~1119) so that once the worker is ready, instead of relying on Twilio's polling `/api/call/wait` to notice `status == "accepted"`, it directly issues the `transfer_call` command when `TELEPHONY_PROVIDER == "telnyx"`:

```python
# Inside call_accept, after _persist_engine_state(active_calls[room_name], worker):
if TELEPHONY_PROVIDER == "telnyx" and telnyx_client:
    call_control_id = active_calls[room_name].get("call_control_id")
    if call_control_id:
        sip_domain = get_livekit_sip_domain()
        from .telnyx_integration import transfer_call
        try:
            await transfer_call(
                telnyx_client,
                call_control_id,
                f"sip:{room_name}@{sip_domain};transport=tcp",
                transport="TCP",
            )
            print(f"[Telnyx] Transfer issued: {room_name}@{sip_domain}")
        except Exception as exc:
            await _cleanup_room_state(room_name, delete_room=True)
            raise HTTPException(status_code=502, detail="Unable to bridge the call to LiveKit.") from exc
```

This is the direct behavioral replacement for Twilio's `/api/call/wait` returning `<Dial><Sip>...` TwiML — same trigger condition (agent accepted, worker ready), same target SIP URI, but pushed as a command instead of returned as markup.

- [ ] **Step 5: Manual verification (no automated test for a live webhook route — documented here for the human running Task 3's setup)**

Once Task 3's Application webhook URL points at a reachable `SERVER_URL` (tunnel for local dev), place one real inbound test call to the Telnyx number and confirm in server logs: `call.initiated` → `[Telnyx Webhook] Answered...` → agent UI shows the incoming-call popup → accepting triggers `[Telnyx] Transfer issued` → the lead hears the converted voice. This is a live test requiring the account from Task 3; do not mark this step done without actually placing the call.

- [ ] **Step 6: Commit**

```bash
git add backend/main.py
git commit -m "feat(telnyx): add event-driven inbound webhook and call-accept transfer"
```

---

### Task 5: Outbound calling + `/api/setup` + `/api/health` provider branching

**Files:**
- Modify: `backend/main.py`

**Interfaces:**
- Consumes: `originate_call`, `start_dual_recording`, `hangup_call` from Task 2.
- Produces: `_find_outbound_trunk` and `dial_outbound` work under either provider; `/api/setup` and `/api/health` report Telnyx state when `TELEPHONY_PROVIDER=telnyx`.

- [ ] **Step 1: Make `_find_outbound_trunk` provider-aware**

Modify (line ~803):

```python
async def _find_outbound_trunk():
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        return None
    trunk_name = "Keira Telnyx Outbound" if TELEPHONY_PROVIDER == "telnyx" else "Keira Twilio Outbound"
    fallback_id = TELNYX_SIP_TRUNK_ID if TELEPHONY_PROVIDER == "telnyx" else TWILIO_SIP_TRUNK_ID
    lk = None
    try:
        lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        trunks = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
        trunk = next((t for t in trunks.items if t.name == trunk_name), None)
        return trunk.sip_trunk_id if trunk else fallback_id
    except Exception as exc:
        print(f"[Server] Live trunk lookup failed: {type(exc).__name__}")
        return fallback_id
    finally:
        if lk:
            await lk.aclose()
```

Note: `dial_outbound` (line ~895) calls `_find_outbound_trunk()` and then `lk.sip.create_sip_participant(...)` — this part is **already provider-agnostic** (it just dials the PSTN number through whichever LiveKit trunk ID comes back) and needs no further change. The lead-side call still goes out over LiveKit's SIP trunk exactly as today; only which trunk (Twilio's or Telnyx's) it resolves to changes.

- [ ] **Step 2: Add the Telnyx branch to `/api/setup`**

Modify `setup_integrations` (line ~1285) — wrap the existing Twilio-specific "Step 1" and "Step 2" blocks in `if TELEPHONY_PROVIDER == "twilio":`, and add a parallel Telnyx branch:

```python
if TELEPHONY_PROVIDER == "telnyx":
    if not TELNYX_SIP_USERNAME or not TELNYX_SIP_PASSWORD:
        errors["livekit_sip_trunk"] = "Missing TELNYX_SIP_USERNAME or TELNYX_SIP_PASSWORD."
    elif not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        errors["livekit_sip_trunk"] = "Missing LIVEKIT_API_KEY or LIVEKIT_API_SECRET."
    else:
        lk = None
        try:
            lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            existing = await lk.sip.list_outbound_trunk(api.ListSIPOutboundTrunkRequest())
            existing_trunk = next((t for t in existing.items if t.name == "Keira Telnyx Outbound"), None)
            if existing_trunk:
                trunk_id = existing_trunk.sip_trunk_id
                results["livekit_sip_trunk"] = {
                    "status": "already_exists",
                    "trunk_id": trunk_id,
                    "note": "Trunk already exists and was reused. TELNYX_SIP_TRUNK_ID env var remains valid.",
                }
            else:
                trunk_info = api.SIPOutboundTrunkInfo(
                    name="Keira Telnyx Outbound",
                    address="sip.telnyx.com",
                    numbers=[TELNYX_PHONE_NUMBER.replace(" ", "")] if TELNYX_PHONE_NUMBER else [],
                    auth_username=TELNYX_SIP_USERNAME,
                    auth_password=TELNYX_SIP_PASSWORD,
                    headers_to_attributes={"X-Telnyx-Username": TELNYX_SIP_USERNAME},
                )
                created = await lk.sip.create_outbound_trunk(
                    api.CreateSIPOutboundTrunkRequest(trunk=trunk_info)
                )
                trunk_id = created.sip_trunk_id
                results["livekit_sip_trunk"] = {
                    "status": "created",
                    "trunk_id": trunk_id,
                    "address": "sip.telnyx.com",
                    "action": f"Save TELNYX_SIP_TRUNK_ID={trunk_id} in your Render env vars then redeploy",
                }
        except Exception as e:
            errors["livekit_sip_trunk"] = str(e)
        finally:
            if lk:
                await lk.aclose()
    # Telnyx's webhook URL is set once on the Call Control Application in the
    # portal (Task 3), not per-number like Twilio's voice_url — no "Step 2"
    # equivalent API call needed here. Note this explicitly in the response:
    results["telnyx_webhook"] = {
        "status": "manual",
        "note": "Telnyx webhook URL is configured on the Call Control Application in the Telnyx portal, not via this endpoint. Confirm it points to " + (f"{SERVER_URL.rstrip('/')}/api/telnyx/webhook" if SERVER_URL else "<SERVER_URL not set>"),
    }
```

Verify the exact `headers_to_attributes` field name against the installed `livekit-server-sdk` version's `SIPOutboundTrunkInfo` proto (`python -c "from livekit import api; print(api.SIPOutboundTrunkInfo.DESCRIPTOR.fields_by_name.keys())"`) before trusting it — this project has been bitten before by exactly this kind of proto field-name mismatch (see `subsystem-notes.md`'s `participant_identity` vs `identity` incident on `UpdateSubscriptionsRequest`).

- [ ] **Step 3: Add the Telnyx branch to `/api/health`**

Modify (line ~1253):

```python
"telnyx": {
    "api_key_set": bool(TELNYX_API_KEY),
    "public_key_set": bool(TELNYX_PUBLIC_KEY),
    "connection_id_set": bool(TELNYX_CONNECTION_ID),
    "phone_number_configured": bool(TELNYX_PHONE_NUMBER),
    "client_initialized": bool(telnyx_client),
},
"telephony_provider": TELEPHONY_PROVIDER,
```

Add this dict alongside (not replacing) the existing `"twilio"` block — both report simultaneously while both providers coexist.

- [ ] **Step 4: Add the Telnyx hangup branch to `_do_end_call`**

Modify (line ~1198), alongside the existing Twilio hangup fallback:

```python
if TELEPHONY_PROVIDER == "telnyx" and telnyx_client and call_info:
    if call_info.get("direction") == "inbound" and "call_control_id" in call_info:
        try:
            from .telnyx_integration import hangup_call
            await hangup_call(telnyx_client, call_info["call_control_id"])
            print(f"[Server] Telnyx call {call_info['call_control_id']} hung up programmatically")
        except Exception as e:
            print(f"[Server Error hanging up Telnyx call] {e}")
```

- [ ] **Step 5: Manual verification**

With `TELEPHONY_PROVIDER=telnyx` and Task 3's account state in `.env`, run `POST /api/setup` locally and confirm `results["livekit_sip_trunk"]["status"]` is `"created"` (or `"already_exists"` on a re-run — verifying the idempotency the Twilio version already has). Save the returned `trunk_id` as `TELNYX_SIP_TRUNK_ID`. Then place one outbound test call via the existing `/api/call/outbound` + `/api/call/outbound/dial` flow (unchanged) and confirm it rings a real phone.

- [ ] **Step 6: Commit**

```bash
git add backend/main.py
git commit -m "feat(telnyx): provider branch for outbound trunk setup, health, and hangup"
```

---

### Task 6: Recording parity + regional edge verification

**Files:**
- Modify: `backend/main.py` (recording trigger point)
- Modify: `.agents/context/subsystem-notes.md` (record the Telnyx AnchorSite/region finding — this is exactly the kind of load-bearing infra gotcha that section exists for, per CLAUDE.md's routing rules)

**Interfaces:**
- Consumes: `start_dual_recording` from Task 2.

- [ ] **Step 1: Add dual-channel recording to the Telnyx inbound answer path**

In Task 4's `telnyx_webhook` handler, right after `await answer_call(...)`:

```python
from .telnyx_integration import start_dual_recording
try:
    await start_dual_recording(telnyx_client, event.call_control_id)
except Exception as exc:
    print(f"[Telnyx Webhook] Recording start failed (non-fatal): {exc}")
```

Non-fatal by design — matches the existing Twilio recording's role as a diagnostic aid (per `.agents/decisions/log.md`'s 2026-07-08 entry: `record-from-answer-dual` was added for the 3-point call-capture diagnostic, not a call-blocking requirement), not a call-blocking requirement.

- [ ] **Step 2: Verify recording is a user-approved live behavior, same as the Twilio precedent**

Per `.agents/decisions/log.md`, enabling Twilio recording required explicit user approval (billing/consent implications). Confirm the same approval applies before this Telnyx equivalent goes live — don't treat Task 3's earlier account-setup confirmation as covering this too; recording is a distinct consent-bearing decision.

- [ ] **Step 3: Resolve the AnchorSite/region question from Task 3 Step 4 and document it**

Using whatever was found in Task 3 Step 4, add a new subsection to `.agents/context/subsystem-notes.md` (append near the existing "Twilio media edge pinning" section, since this is the direct Telnyx analog of that finding):

```markdown
## Telnyx media edge/AnchorSite (added <date>)
- Telnyx separates SIP Region (signaling) from AnchorSite (media anchor) —
  unlike Twilio's single combined per-region termination domain. Render/LiveKit
  are in Singapore; getting this wrong risks the same class of transpacific
  RTP-path bug Twilio's `pstn.twilio.com` (non-geo-routing) caused — see the
  "Twilio media edge pinning" section above for the full incident this rhymes with.
- <Record here: was a Singapore/APAC AnchorSite available in the portal? What
  was selected? Confirmed via one test call's RTP path, or still open?>
```

This step cannot be completed with a placeholder — it requires Task 3 Step 4's actual portal finding. If Task 3 hasn't been run yet when this task is reached, stop and complete Task 3 first rather than writing a guess into subsystem-notes.md (a memory/notes file with a wrong infra fact has bitten this project before, per the region-mismatch history already in that file).

- [ ] **Step 4: Commit**

```bash
git add backend/main.py .agents/context/subsystem-notes.md
git commit -m "feat(telnyx): dual-channel recording parity; document AnchorSite/region finding"
```

---

### Task 7: Cutover — make Telnyx the default, retire Twilio code

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/requirements.txt`
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Interfaces:** None new — this is a deletion/cleanup task, run only after Tasks 1-6 are live-verified end-to-end (both inbound and outbound test calls placed successfully over Telnyx).

- [ ] **Step 1: Stop and get explicit user confirmation**

This task deletes the Twilio integration entirely and flips the default provider. Confirm the user has actually placed and confirmed working inbound + outbound test calls over Telnyx (Task 4 Step 5, Task 5 Step 5) before proceeding — do not cut over on "the code looks right."

- [ ] **Step 2: Flip the default and remove the provider-branch conditionals**

Change `TELEPHONY_PROVIDER = os.getenv("TELEPHONY_PROVIDER", "twilio")` to default `"telnyx"`, then simplify: remove the `if TELEPHONY_PROVIDER == "twilio": / elif == "telnyx":` branches added in Tasks 4-5, keeping only the Telnyx code paths as unconditional. This is a straightforward deletion pass through everything Task 4/5 touched.

- [ ] **Step 3: Delete Twilio-specific code**

Remove: `require_twilio_signature`, the `TWILIO_*` env vars and `twilio_client` init block, `/api/call/inbound`, `/api/call/wait`, `/api/call/status-event` (Telnyx's `call.hangup` webhook event replaces the status-callback's role), the Twilio hangup fallback in `_do_end_call`, and the Twilio block in `/api/health`. Remove `twilio` from `backend/requirements.txt`.

- [ ] **Step 4: Update docs**

Update `CLAUDE.md`'s "Media/Telephony" line and env-var references, and `README.md` §3's environment variable reference table, replacing `TWILIO_*` entries with the `TELNYX_*` ones. Update the "Telephony & SIP" section's prose (currently describes `POST /api/setup` provisioning Twilio trunks).

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest backend/ -v` and `python -m pytest modal_deploy/ -v`
Expected: all pass, including the new `backend/test_telnyx_integration.py`, with no lingering imports of the deleted Twilio code.

- [ ] **Step 6: Commit**

```bash
git add backend/main.py backend/requirements.txt CLAUDE.md README.md
git commit -m "feat(telnyx): cut over to Telnyx as sole PSTN provider, remove Twilio integration"
```

---

## Self-Review Notes

- **Spec coverage**: signature verification ✅ (Task 1), webhook event handling ✅ (Task 4), outbound origination ✅ (Task 2/5), SIP trunk config for LiveKit's outbound leg ✅ (Task 3/5), recording parity ✅ (Task 6), regional edge risk ✅ (Task 3/6), cutover/cleanup ✅ (Task 7).
- **Deliberately deferred to live verification, not guessed into code**: exact `telnyx` SDK method names/client class (Task 1 Step 1, Task 2 Step 7 note), `headers_to_attributes` proto field name (Task 5 Step 2 note), AnchorSite/Singapore availability (Task 3 Step 4, gates Task 6 Step 3). These are flagged inline rather than hardcoded because getting them wrong silently is exactly the failure mode `subsystem-notes.md` already documents twice for this codebase (the `participant_identity`/`identity` proto trap, and the Twilio edge-routing miss).
- **Everything downstream of the SIP participant joining the LiveKit room — `pipeline.py`, RVC/Modal, the playout buffer, SOLA, `_restrict_sip_audio` — is untouched by every task above.** No task should ever need to open those files; if implementation reveals otherwise, stop and re-scope rather than proceeding.
