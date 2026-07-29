const { Room, RoomEvent, Track } = LivekitClient;

const API_BASE = '';

function randomSuffix() {
  return Math.random().toString(36).slice(2, 8);
}

async function fetchToken(roomName, identity) {
  const gender = document.getElementById('gender-select').value;
  const resp = await fetch(`${API_BASE}/api/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ roomName, identity, role: 'agent', agentGender: gender }),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `Token request failed: HTTP ${resp.status}`);
  }
  return resp.json();
}

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

let relayRoom = null;
let relayMicTrack = null;
let relayAudioEl = null;
let meterInterval = null;

async function startRelay() {
  const roomNameInput = document.getElementById('room-name');
  const statusEl = document.getElementById('relay-status');
  const startBtn = document.getElementById('btn-start');
  const stopBtn = document.getElementById('btn-stop');

  startBtn.disabled = true;
  statusEl.textContent = 'Requesting token…';

  try {
    const roomName = roomNameInput.value.trim();
    const identity = `agent-relay-${randomSuffix()}`;
    const { token, serverUrl } = await fetchToken(roomName, identity);

    relayRoom = new Room({ adaptiveStream: true, dynacast: true });

    relayRoom.on(RoomEvent.TrackSubscribed, async (track, publication, participant) => {
      if (!participant.identity.startsWith('voice-converter-bot')) return;

      relayAudioEl = track.attach();
      relayAudioEl.autoplay = true;
      document.body.appendChild(relayAudioEl);

      const deviceId = document.getElementById('output-device').value;
      if (deviceId && deviceId !== '__default' && typeof relayAudioEl.setSinkId === 'function') {
        try {
          await relayAudioEl.setSinkId(deviceId);
          statusEl.textContent = `Relay active — routing to "${getSelectedDeviceLabel()}". Speak now.`;
        } catch (err) {
          statusEl.textContent = `Connected, but setSinkId failed: ${err.message}. Playing through default device.`;
        }
      } else {
        statusEl.textContent = 'Relay active — playing through default device. Speak now.';
      }

      try {
        await relayAudioEl.play();
      } catch (_) {
        relayAudioEl.muted = true;
        await relayAudioEl.play().catch(() => {});
        relayAudioEl.muted = false;
      }

      startMeters();
    });

    relayRoom.on(RoomEvent.TrackUnsubscribed, (track) => {
      track.detach();
    });

    statusEl.textContent = 'Connecting to LiveKit…';
    await relayRoom.connect(serverUrl, token);
    await relayRoom.startAudio();

    statusEl.textContent = 'Publishing microphone…';
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { noiseSuppression: false, autoGainControl: false, echoCancellation: true },
    });
    relayMicTrack = await relayRoom.localParticipant.publishTrack(
      stream.getAudioTracks()[0],
      { name: 'microphone', source: Track.Source.Microphone },
    );

    startMeters();

    statusEl.textContent = 'Starting voice-conversion bot…';
    await startBot(roomName, identity);

    statusEl.textContent = `Connected as ${identity}. Waiting for bot's converted track…`;
    stopBtn.disabled = false;
  } catch (err) {
    statusEl.textContent = `Failed: ${err.message}`;
    startBtn.disabled = false;
    if (relayRoom) { await relayRoom.disconnect(); relayRoom = null; }
  }
}

async function stopRelay() {
  const statusEl = document.getElementById('relay-status');
  const startBtn = document.getElementById('btn-start');
  const stopBtn = document.getElementById('btn-stop');

  stopMeters();
  if (relayRoom) {
    await relayRoom.disconnect();
    relayRoom = null;
  }
  relayMicTrack = null;
  if (relayAudioEl) {
    relayAudioEl.remove();
    relayAudioEl = null;
  }
  statusEl.textContent = 'Stopped.';
  startBtn.disabled = false;
  stopBtn.disabled = true;
}

document.getElementById('btn-start').addEventListener('click', startRelay);
document.getElementById('btn-stop').addEventListener('click', stopRelay);

function startMeters() {
  if (meterInterval) return;
  meterInterval = setInterval(() => {
    if (relayMicTrack) {
      const level = Math.round(relayMicTrack.getAudioLevel() * 100);
      document.getElementById('input-meter').style.width = `${level}%`;
      document.getElementById('input-db').textContent = `${level}%`;
    }
  }, 200);
}

function stopMeters() {
  if (meterInterval) {
    clearInterval(meterInterval);
    meterInterval = null;
  }
  document.getElementById('input-meter').style.width = '0%';
  document.getElementById('input-db').textContent = '--';
  document.getElementById('output-meter').style.width = '0%';
}

function getSelectedDeviceLabel() {
  const select = document.getElementById('output-device');
  return select.selectedOptions[0]?.textContent || 'unknown device';
}

async function populateOutputDevices() {
  const select = document.getElementById('output-device');
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
  } catch (err) {
    console.warn('Mic permission prompt for device labels was denied:', err);
  }

  select.replaceChildren();

  const defaultOpt = document.createElement('option');
  defaultOpt.value = '__default';
  defaultOpt.textContent = 'Default speakers / headphones (for testing)';
  select.appendChild(defaultOpt);

  const devices = await navigator.mediaDevices.enumerateDevices();
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
