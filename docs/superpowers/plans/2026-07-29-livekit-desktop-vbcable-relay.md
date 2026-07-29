# LiveKit Desktop-to-VB-Cable Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a new, standalone browser page where a desktop user speaks into their real
microphone, has their voice converted by the existing `VoiceConversionWorker` (the exact
code path already proven on real PSTN calls), and hears the converted output on a *second*
browser tab/window whose audio output device is pinned to a virtual audio cable (e.g.
VB-Cable), so a third-party application (e.g. WhatsApp Desktop) can pick up the converted
voice as its own "microphone" input.

**Architecture:** Two LiveKit room participants, both browser tabs, no new backend audio
pipeline. Tab A ("agent" role) joins a LiveKit room and publishes the real mic exactly like
`frontend/app.js`'s existing `publishAgentMicrophone`. The existing `_do_start_bot` spawns
the bot into that same room (unchanged). Tab B ("listener" role) joins the same room,
subscribes to the bot's converted output track, and attaches it to an `<audio>` element
whose `setSinkId()` is pointed at whatever output device the OS exposes for the virtual
cable (VB-Cable's virtual "input" line, which Windows lists as a playback/output device).
The only backend change is teaching `/api/token` to mint a **subscribe-only** token for a
non-"agent" identity; everything else (the bot, the converter, noise suppression, adaptive
pitch) is reused with zero changes, per the user's explicit decision not to touch
`backend/desktop_audio.py` or delete the older custom desktop path in this plan.

**Tech Stack:** FastAPI (existing `backend/main.py`), LiveKit Python SDK (already imported
as `from livekit import api`), LiveKit JS `Room`/`RoomEvent`/`Track` client (already loaded
globally as `LivekitClient` in the existing frontend — see `frontend/app.js:2`), vanilla
HTML/ES6 (project convention — no framework), `HTMLMediaElement.setSinkId` (Chromium-only
device output routing API).

## Global Constraints

- Do not modify `frontend/app.js` or `frontend/desktop/desktop.js` — this is a new,
  standalone page, per explicit user decision.
- Do not modify or remove `backend/desktop_audio.py`, its WS route, or its tests — the
  older custom desktop path stays as-is, untouched, per explicit user decision.
- Reuse `/api/token` (extended with a `role` field) rather than adding a new endpoint — per
  explicit user decision.
- Never change `_do_start_bot`, `VoiceConversionWorker`, `RVCStreamingConverter`, or any
  noise-suppression/pitch-lock code — the entire point of this feature is zero changes to
  the proven conversion pipeline.
- Follow existing code style: Python PEP 8 with type hints; vanilla ES6, no build step, no
  new frontend dependencies.
- New backend Pydantic request fields must use the same validation pattern already used in
  `backend/main.py` (`Literal[...]`, `Field(pattern=...)`) — not ad hoc string checks.

---

## File Structure

- **Modify: `backend/main.py`**
  - `TokenRequest` model gains an optional `role: Literal["agent", "listener"] = "agent"`
    field.
  - `get_token` (`POST /api/token`) branches identity validation and LiveKit grants by
    `role`: `"agent"` keeps today's behavior (`_require_agent_identity`, publish+subscribe);
    `"listener"` uses a new, separate identity validator that does *not* require the
    substring `"agent"`, and grants `can_publish=False, can_subscribe=True`.
- **Modify: `backend/security.py`**
  - Add `validate_listener_identity(value: str) -> bool` alongside the existing
    `validate_agent_identity` — same character-safety regex, no `"agent"`-substring
    requirement, since a listener identity is semantically not an agent.
- **Create: `backend/test_token_roles.py`**
  - Unit tests (FastAPI `TestClient`, following the existing pattern in
    `backend/test_desktop_audio.py`) covering: default role is `"agent"` (back-compat),
    `"listener"` role accepts a non-"agent" identity, `"listener"` role token grants
    `can_publish=False`/`can_subscribe=True`, `"agent"` role still rejects an identity
    without `"agent"` in it.
- **Create: `frontend/livekit-relay/index.html`**
  - New standalone page. Loads the LiveKit JS SDK the same way `frontend/app.js`'s host
    page already does (check `frontend/index.html` for the exact `<script>` tag/CDN or
    vendored path and copy it verbatim — do not guess a different LiveKit JS version).
    Two clearly separate sections: "Agent (speak here)" and "Listener (route to virtual
    cable)", each with its own Join/Leave button and status text.
- **Create: `frontend/livekit-relay/relay.js`**
  - All logic for both roles. No shared state with `app.js`/`desktop.js` — this file is
    fully self-contained.
- **Modify: `backend/main.py`** (static hosting)
  - Confirm the existing `StaticFiles` mount already serves anything under `frontend/`
    recursively (check how `frontend/desktop/` is currently served) and add the new
    `frontend/livekit-relay/` path only if the existing mount doesn't already cover it.

---

## Task 1: Add a `listener` role to the `/api/token` broker

**Files:**
- Modify: `backend/security.py`
- Modify: `backend/main.py:710-759` (the `TokenRequest` model and `get_token` handler)
- Test: `backend/test_token_roles.py` (new file)

**Interfaces:**
- Consumes: existing `api.AccessToken`, `api.VideoGrants` (from `livekit.api`, already
  imported in `backend/main.py:25`), existing `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`/
  `LIVEKIT_URL` module-level constants.
- Produces: `validate_listener_identity(value: str) -> bool` in `backend/security.py`,
  importable as `from .security import validate_listener_identity`. `POST /api/token`
  accepts `{"roomName": str, "identity": str, "role": "agent"|"listener", "agentGender":
  ..., "voiceEngine": ...}` and returns `{"token": str, "serverUrl": str}` — same response
  shape as today, unchanged.

- [ ] **Step 1: Write the failing tests**

Create `backend/test_token_roles.py`:

```python
import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("backend.main.LIVEKIT_API_KEY", "test-key")
    monkeypatch.setattr("backend.main.LIVEKIT_API_SECRET", "test-secret-0123456789abcdef")
    monkeypatch.setattr("backend.main.LIVEKIT_URL", "wss://example.livekit.cloud")
    return TestClient(app)


def test_token_defaults_to_agent_role(client):
    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "agent-desktop-1",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert body["serverUrl"] == "wss://example.livekit.cloud"


def test_agent_role_still_requires_agent_in_identity(client):
    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "desktop-1",
        "role": "agent",
    })
    assert resp.status_code == 422


def test_listener_role_accepts_non_agent_identity(client):
    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "vbcable-listener-1",
        "role": "listener",
    })
    assert resp.status_code == 200
    assert "token" in resp.json()


def test_listener_role_rejects_unsafe_identity(client):
    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "listener with spaces",
        "role": "listener",
    })
    assert resp.status_code == 422


def test_listener_role_grants_subscribe_only(client, monkeypatch):
    captured = {}
    import backend.main as main_module

    original_access_token = main_module.api.AccessToken

    class RecordingAccessToken(original_access_token):
        def with_grants(self, grants):
            captured["grants"] = grants
            return super().with_grants(grants)

    monkeypatch.setattr(main_module.api, "AccessToken", RecordingAccessToken)

    resp = client.post("/api/token", json={
        "roomName": "relay_test_room",
        "identity": "vbcable-listener-1",
        "role": "listener",
    })
    assert resp.status_code == 200
    assert captured["grants"].can_publish is False
    assert captured["grants"].can_subscribe is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest backend/test_token_roles.py -v`
Expected: FAIL — `role` field doesn't exist yet on `TokenRequest`, so requests with
`"role": "listener"` are silently ignored by Pydantic (extra fields ignored by default) and
every request currently takes the `"agent"` path, so `test_listener_role_accepts_non_agent_identity`
and `test_listener_role_grants_subscribe_only` should fail (422 / wrong grants).

- [ ] **Step 3: Add `validate_listener_identity` to `backend/security.py`**

Read `backend/security.py:1-20` first to match the existing `_IDENTITY_RE` pattern exactly,
then add directly below `validate_agent_identity`:

```python
def validate_listener_identity(value: str) -> bool:
    return bool(_IDENTITY_RE.fullmatch(value or ""))
```

- [ ] **Step 4: Update `backend/main.py`'s imports and `TokenRequest` model**

Find the existing import line (`backend/main.py`, search for `from .security import`) and
add `validate_listener_identity` to it. Then modify `TokenRequest`:

```python
class TokenRequest(BaseModel):
    roomName: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    identity: str = Field(min_length=5, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    role: Literal["agent", "listener"] = "agent"
    agentGender: Literal["male", "female"] = "male"
    voiceEngine: Literal["rvc"] = "rvc"
```

- [ ] **Step 5: Branch `get_token` on `role`**

Replace the body of `get_token` (`backend/main.py:733-759`):

```python
@app.post("/api/token")
async def get_token(request: TokenRequest):
    """
    Brokers LiveKit client access tokens for browser web clients.
    `role="agent"` (default) is the existing agent-dashboard/mic-publishing path,
    unchanged. `role="listener"` is subscribe-only, for a participant that only
    plays back another participant's track (e.g. routing the bot's converted
    audio to a virtual audio cable) and never publishes its own mic.
    """
    if request.role == "agent":
        _require_agent_identity(request.identity)
        can_publish, can_subscribe = True, True
    else:
        if not validate_listener_identity(request.identity):
            raise HTTPException(
                status_code=422,
                detail="identity must be 5-64 safe characters.",
            )
        can_publish, can_subscribe = False, True

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
                can_publish=can_publish,
                can_subscribe=can_subscribe,
            )) \
            .with_ttl(datetime.timedelta(seconds=3600))

        return {"token": token.to_jwt(), "serverUrl": LIVEKIT_URL}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest backend/test_token_roles.py -v`
Expected: PASS, all 5 tests.

- [ ] **Step 7: Run the full existing backend test suite to confirm no regression**

Run: `python -m pytest backend/ -v`
Expected: PASS — in particular, confirm no existing test asserted on `TokenRequest`'s exact
field set or `get_token`'s old unconditional-agent-validation behavior in a way this change
would break (the default `role="agent"` is designed to keep exact backward compatibility).

- [ ] **Step 8: Commit**

```bash
git add backend/main.py backend/security.py backend/test_token_roles.py
git commit -m "Add subscribe-only listener role to /api/token broker"
```

---

## Task 2: Build the standalone two-role relay page (frontend)

**Files:**
- Create: `frontend/livekit-relay/index.html`
- Create: `frontend/livekit-relay/relay.js`
- Read (do not modify): `frontend/index.html` (to copy the exact LiveKit SDK `<script>`
  include), `frontend/app.js:338-373,499-538` (to mirror the proven room-connect and
  mic-publish pattern), `backend/main.py` (find the `StaticFiles` mount call — search for
  `StaticFiles(directory=` — to confirm `frontend/livekit-relay/` is served without a
  backend change; if the mount is not recursive/catch-all, add the minimal route needed,
  matching how `frontend/desktop/` is currently exposed).

**Interfaces:**
- Consumes: `POST /api/token` with body `{roomName, identity, role, agentGender,
  voiceEngine}` → `{token, serverUrl}` (Task 1's contract). Global `LivekitClient` object
  providing `Room`, `RoomEvent`, `Track` (same globals `frontend/app.js:2` destructures).
- Produces: a page reachable at `/livekit-relay/` (or wherever the static mount serves it)
  with two independent join/leave flows that do not depend on any code in `app.js` or
  `desktop.js`.

- [ ] **Step 1: Read the exact LiveKit JS include and static-mount pattern**

Open `frontend/index.html` and note the exact `<script src="...">` tag (or vendored path)
used to load the LiveKit JS client. Open `backend/main.py` and search for
`app.mount(` / `StaticFiles(` to see how `frontend/` is currently served, and whether
`frontend/desktop/` needed its own explicit mount line or was already covered. Copy
whichever pattern applies so the new page loads the SDK identically and is servable
without guessing.

- [ ] **Step 2: Write `frontend/livekit-relay/index.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Keira — LiveKit Desktop Relay</title>
  <!-- COPY THE EXACT SCRIPT TAG FROM frontend/index.html HERE, DO NOT GUESS A CDN URL -->
  <style>
    body { font-family: system-ui, sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; }
    section { border: 1px solid #ccc; border-radius: 8px; padding: 1rem; margin-bottom: 1.5rem; }
    h2 { margin-top: 0; }
    button { padding: 0.5rem 1rem; font-size: 1rem; cursor: pointer; }
    .status { font-family: monospace; white-space: pre-wrap; margin-top: 0.5rem; }
    select { width: 100%; padding: 0.4rem; margin-top: 0.5rem; }
  </style>
</head>
<body>
  <h1>LiveKit Desktop Relay</h1>
  <p>Room name: <input id="room-name" value="relay-desktop" style="font-family: monospace;"></p>

  <section>
    <h2>1. Agent (speak here)</h2>
    <p>Joins the room and publishes your real microphone — same capture settings as the
      live agent dashboard (no browser noise suppression/AGC, since those strip detail the
      converter needs).</p>
    <button id="btn-join-agent">Join as Agent</button>
    <button id="btn-leave-agent" disabled>Leave</button>
    <div id="agent-status" class="status">Not connected.</div>
  </section>

  <section>
    <h2>2. Listener (route to virtual cable)</h2>
    <p>Joins the same room, subscribes to the bot's converted output only, and plays it to
      the output device you pick below. Point this at your virtual audio cable's input
      device so WhatsApp (set to use the cable's output as its microphone) receives the
      converted voice.</p>
    <select id="output-device"></select>
    <button id="btn-join-listener">Join as Listener</button>
    <button id="btn-leave-listener" disabled>Leave</button>
    <div id="listener-status" class="status">Not connected.</div>
  </section>

  <script src="relay.js"></script>
</body>
</html>
```

- [ ] **Step 3: Write `frontend/livekit-relay/relay.js` — shared token-fetch helper**

```javascript
const { Room, RoomEvent, Track } = LivekitClient;

const API_BASE = '';

async function fetchToken(roomName, identity, role) {
  const resp = await fetch(`${API_BASE}/api/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ roomName, identity, role }),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Token request failed: HTTP ${resp.status}`);
  }
  return resp.json();
}

function randomSuffix() {
  return Math.random().toString(36).slice(2, 8);
}
```

- [ ] **Step 4: Write the Agent-role join/leave logic**

Append to `relay.js`:

```javascript
let agentRoom = null;
let agentMicTrack = null;

async function joinAsAgent() {
  const roomNameInput = document.getElementById('room-name');
  const statusEl = document.getElementById('agent-status');
  const joinBtn = document.getElementById('btn-join-agent');
  const leaveBtn = document.getElementById('btn-leave-agent');

  joinBtn.disabled = true;
  statusEl.textContent = 'Requesting token…';

  try {
    const roomName = roomNameInput.value.trim();
    const identity = `agent-relay-${randomSuffix()}`;
    const { token, serverUrl } = await fetchToken(roomName, identity, 'agent');

    agentRoom = new Room({ adaptiveStream: true, dynacast: true });
    statusEl.textContent = 'Connecting to LiveKit…';
    await agentRoom.connect(serverUrl, token);

    // Publish with browser noiseSuppression/AGC OFF — see frontend/app.js's
    // publishAgentMicrophone for why: those strip detail the RVC model needs.
    // echoCancellation stays on.
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { noiseSuppression: false, autoGainControl: false, echoCancellation: true },
    });
    agentMicTrack = await agentRoom.localParticipant.publishTrack(
      stream.getAudioTracks()[0],
      { name: 'microphone', source: Track.Source.Microphone },
    );

    statusEl.textContent = `Connected as ${identity} in room "${roomName}". Speak now.`;
    leaveBtn.disabled = false;
  } catch (err) {
    statusEl.textContent = `Failed: ${err.message}`;
    joinBtn.disabled = false;
  }
}

async function leaveAsAgent() {
  const statusEl = document.getElementById('agent-status');
  const joinBtn = document.getElementById('btn-join-agent');
  const leaveBtn = document.getElementById('btn-leave-agent');

  if (agentRoom) {
    await agentRoom.disconnect();
    agentRoom = null;
  }
  agentMicTrack = null;
  statusEl.textContent = 'Not connected.';
  joinBtn.disabled = false;
  leaveBtn.disabled = true;
}

document.getElementById('btn-join-agent').addEventListener('click', joinAsAgent);
document.getElementById('btn-leave-agent').addEventListener('click', leaveAsAgent);
```

- [ ] **Step 5: Write the output-device picker (populated on load)**

Append to `relay.js`:

```javascript
async function populateOutputDevices() {
  const select = document.getElementById('output-device');
  try {
    // Device labels are only populated after a permission prompt has been
    // granted at least once in this origin — requesting mic access here
    // (even though the listener role never publishes it) is what unlocks
    // real device labels instead of "Speaker 1", "Speaker 2", etc.
    await navigator.mediaDevices.getUserMedia({ audio: true }).then(
      (s) => s.getTracks().forEach((t) => t.stop())
    );
  } catch (err) {
    console.warn('Mic permission prompt for device labels was denied:', err);
  }

  const devices = await navigator.mediaDevices.enumerateDevices();
  select.replaceChildren();
  devices
    .filter((d) => d.kind === 'audiooutput')
    .forEach((d) => {
      const option = document.createElement('option');
      option.value = d.deviceId;
      option.textContent = d.label || `Output ${d.deviceId.slice(0, 8)}`;
      select.appendChild(option);
    });
}

populateOutputDevices();
```

- [ ] **Step 6: Write the Listener-role join/leave logic, subscribing only to the bot's track**

Append to `relay.js`:

```javascript
let listenerRoom = null;
let listenerAudioEl = null;

async function joinAsListener() {
  const roomNameInput = document.getElementById('room-name');
  const outputSelect = document.getElementById('output-device');
  const statusEl = document.getElementById('listener-status');
  const joinBtn = document.getElementById('btn-join-listener');
  const leaveBtn = document.getElementById('btn-leave-listener');

  joinBtn.disabled = true;
  statusEl.textContent = 'Requesting token…';

  try {
    const roomName = roomNameInput.value.trim();
    const identity = `vbcable-listener-${randomSuffix()}`;
    const { token, serverUrl } = await fetchToken(roomName, identity, 'listener');

    listenerRoom = new Room({ adaptiveStream: true, dynacast: true });

    listenerRoom.on(RoomEvent.TrackSubscribed, async (track, publication, participant) => {
      // Only play the bot's converted output — never a raw agent mic track,
      // even though a listener token can't publish, another listener's own
      // mic could theoretically show up in a shared room.
      if (!participant.identity.startsWith('voice-converter-bot')) return;

      listenerAudioEl = track.attach();
      listenerAudioEl.autoplay = true;
      document.body.appendChild(listenerAudioEl);

      const deviceId = outputSelect.value;
      if (deviceId && typeof listenerAudioEl.setSinkId === 'function') {
        try {
          await listenerAudioEl.setSinkId(deviceId);
        } catch (err) {
          statusEl.textContent = `Connected, but setSinkId failed: ${err.message}`;
          return;
        }
      }

      try {
        await listenerAudioEl.play();
        statusEl.textContent = `Playing converted audio from ${participant.identity} → device "${outputSelect.selectedOptions[0]?.textContent}".`;
      } catch (err) {
        statusEl.textContent = `Playback blocked by browser autoplay policy: ${err.message}`;
      }
    });

    listenerRoom.on(RoomEvent.TrackUnsubscribed, (track) => {
      track.detach();
    });

    statusEl.textContent = 'Connecting to LiveKit…';
    await listenerRoom.connect(serverUrl, token);
    await listenerRoom.startAudio();
    statusEl.textContent = `Connected as ${identity}. Waiting for the bot's track…`;
    leaveBtn.disabled = false;
  } catch (err) {
    statusEl.textContent = `Failed: ${err.message}`;
    joinBtn.disabled = false;
  }
}

async function leaveAsListener() {
  const statusEl = document.getElementById('listener-status');
  const joinBtn = document.getElementById('btn-join-listener');
  const leaveBtn = document.getElementById('btn-leave-listener');

  if (listenerRoom) {
    await listenerRoom.disconnect();
    listenerRoom = null;
  }
  if (listenerAudioEl) {
    listenerAudioEl.remove();
    listenerAudioEl = null;
  }
  statusEl.textContent = 'Not connected.';
  joinBtn.disabled = false;
  leaveBtn.disabled = true;
}

document.getElementById('btn-join-listener').addEventListener('click', joinAsListener);
document.getElementById('btn-leave-listener').addEventListener('click', leaveAsListener);
```

- [ ] **Step 7: Serve the new page (backend static mount, if needed)**

Based on what Step 1 found: if `frontend/`'s existing `StaticFiles` mount already serves
nested directories generically (most `StaticFiles(directory="frontend")`-style mounts do),
no backend change is needed — skip this step. If `frontend/desktop/` required an explicit
additional mount/route to be reachable, add the equivalent one-line mount for
`frontend/livekit-relay/` in `backend/main.py`, following that exact existing pattern.

- [ ] **Step 8: Manual browser verification (no automated test — this is a device-routing/UI feature)**

Run the server (`uvicorn backend.main:app --reload --port 8000`), open the new page in two
separate tabs (or one tab + one separate browser window):
1. In Tab A, click "Join as Agent". Grant mic permission. Confirm status shows "Connected…
   Speak now."
2. Separately, trigger the bot into the same room name — either via the existing
   `/api/start-bot` endpoint (`curl -X POST http://localhost:8000/api/start-bot -H
   "Content-Type: application/json" -d '{"roomName":"relay-desktop","identity":"agent-relay-test"}'`,
   matching whatever room name you typed in the page) or confirm whether Task 1/2 needs the
   bot auto-started — check how `frontend/app.js`'s outbound-call flow triggers
   `_do_start_bot` and decide if this page should also call `/api/start-bot` automatically
   after the agent joins, rather than requiring a manual curl. If so, add that call at the
   end of `joinAsAgent()` before the final status update.
3. In Tab B, select an output device from the dropdown (a virtual cable's input line if
   testing the full VB-Cable path, or just "default" for a first smoke test), click "Join
   as Listener".
4. Speak into Tab A's mic. Confirm Tab B's status updates to "Playing converted audio from
   voice-converter-bot-… → device …" and that audio is audible (or, with a virtual cable
   selected, that the OS/virtual-cable app shows incoming signal).
5. Confirm leaving either tab cleanly disconnects (buttons re-enable, status resets) with
   no console errors.

- [ ] **Step 9: Commit**

```bash
git add frontend/livekit-relay/
git commit -m "Add standalone LiveKit relay page for desktop-to-virtual-cable voice conversion"
```

---

## Task 3: Auto-start the bot from the Agent join flow (close the manual-curl gap)

This task exists because Task 2 Step 8 identified that a human would otherwise need a
manual `curl` to `/api/start-bot` for every test session — not acceptable for a page meant
to be used repeatedly. This wires the existing, unchanged `/api/start-bot` endpoint into
the Agent join flow.

**Files:**
- Modify: `frontend/livekit-relay/relay.js` (the `joinAsAgent` function from Task 2)

**Interfaces:**
- Consumes: `POST /api/start-bot` with body `{roomName, identity, agentGender,
  voiceEngine}` (existing `TokenRequest`-shaped body per `backend/main.py:761-767`;
  `identity` here MUST satisfy `_require_agent_identity`, i.e. must contain "agent" — reuse
  the same `identity` string already used for the agent's own `/api/token` call in this
  same function, which already does).

- [ ] **Step 1: Add a `startBot` helper to `relay.js`**

```javascript
async function startBot(roomName, identity) {
  const resp = await fetch(`${API_BASE}/api/start-bot`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ roomName, identity }),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `start-bot failed: HTTP ${resp.status}`);
  }
  return resp.json();
}
```

- [ ] **Step 2: Call it from `joinAsAgent`, after publishing the mic track**

In the `joinAsAgent` function written in Task 2 Step 4, insert this right after the
`publishTrack` call and before the final `statusEl.textContent = 'Connected as...'` line:

```javascript
    statusEl.textContent = 'Starting the voice-conversion bot…';
    await startBot(roomName, identity);
```

- [ ] **Step 3: Manual verification**

Repeat Task 2 Step 8's manual test, but skip the manual curl in step 2 — confirm the bot
joins automatically as soon as "Join as Agent" completes (watch server logs for
`[Worker] Connecting to room: ...` / `[Worker] Connected. Identity: ...`).

- [ ] **Step 4: Commit**

```bash
git add frontend/livekit-relay/relay.js
git commit -m "Auto-start the conversion bot when the relay page's Agent joins"
```

---

## Self-Review Notes

- **Spec coverage:** Two-participant LiveKit topology (✅ Task 2/3), reuse of
  `VoiceConversionWorker`/`_do_start_bot` unchanged (✅ Task 3, no modification to
  `pipeline.py` anywhere in this plan), `/api/token` extended with `role` instead of a new
  endpoint (✅ Task 1, per explicit user decision), `desktop_audio.py` left untouched (✅
  no task references or modifies it), output routed to an OS-selectable device via
  `setSinkId` for the VB-Cable/WhatsApp handoff (✅ Task 2 Step 6) — VB-Cable installation
  and WhatsApp's own mic-device selection are OS/third-party-app configuration outside this
  codebase's scope and are not tasks here, consistent with earlier discussion in this
  conversation.
- **Placeholder scan:** no TBD/"add error handling"/"similar to Task N" — Task 2 Step 8 and
  Task 3 intentionally surface a genuine open decision (whether bot autostart belongs in
  Task 2 or a separate Task 3) rather than hiding it, and Task 3 resolves it concretely with
  real code, not a placeholder.
- **Type/name consistency:** `role` field name, `"agent"`/`"listener"` literal values, and
  `fetchToken(roomName, identity, role)`'s signature are used identically across Task 1's
  backend contract and Task 2/3's frontend calls. `voice-converter-bot-` prefix in Task 2
  Step 6 matches the exact prefix `_do_start_bot` constructs
  (`bot_identity = f"voice-converter-bot-{room_name}"`, confirmed in `backend/main.py`).
