const { Room, RoomEvent, Track } = LivekitClient;

const API_BASE = '';

function randomSuffix() {
  return Math.random().toString(36).slice(2, 8);
}

function setState(state) {
  const el = document.getElementById('connection-state');
  el.textContent = state;
  el.dataset.state = state;
}

function setError(msg) {
  const el = document.getElementById('page-error');
  if (msg) { el.textContent = msg; el.hidden = false; }
  else { el.hidden = true; }
}

function updateMeter(id, value) {
  document.getElementById(`${id}-meter-fill`).style.width = `${value}%`;
  document.getElementById(`${id}-meter-value`).textContent = `${value}%`;
}

let relayRoom = null;
let relayMicTrack = null;
let relayAudioEl = null;
let meterInterval = null;
let startedAt = null;

async function fetchToken(roomName, identity) {
  const gender = document.getElementById('voice-profile').value;
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

async function warmGpu() {
  const result = document.getElementById('warm-gpu-result');
  const btn = document.getElementById('warm-gpu');
  btn.disabled = true;
  result.textContent = 'Warming GPU (cold start can take a few minutes)…';
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 7 * 60 * 1000);
  try {
    const resp = await fetch(`${API_BASE}/api/warmup`, { method: 'POST', signal: controller.signal });
    const data = await resp.json().catch(() => ({}));
    result.textContent = data.status === 'success' ? 'GPU warm and ready.' : `GPU warmup: ${data.message || `HTTP ${resp.status}`}`;
  } catch (err) {
    result.textContent = err.name === 'AbortError' ? 'GPU warmup timed out. Try again.' : `GPU warmup failed: ${err.message}`;
  } finally {
    clearTimeout(timeoutId);
    btn.disabled = false;
  }
}

async function startRelay() {
  const statusEl = document.getElementById('connection-state');
  const startBtn = document.getElementById('start-relay');
  const stopBtn = document.getElementById('stop-relay');

  setError(null);
  startBtn.disabled = true;
  setState('starting');

  try {
    const roomName = document.getElementById('room-name').value.trim();
    const identity = `agent-relay-${randomSuffix()}`;
    const { token, serverUrl } = await fetchToken(roomName, identity);

    relayRoom = new Room({ adaptiveStream: true, dynacast: true });

    relayRoom.on(RoomEvent.TrackSubscribed, async (track, publication, participant) => {
      if (!participant.identity.startsWith('voice-converter-bot')) return;

      document.getElementById('bot-identity').textContent = participant.identity;

      relayAudioEl = track.attach();
      relayAudioEl.autoplay = true;
      document.body.appendChild(relayAudioEl);

      const deviceId = document.getElementById('output-device').value;
      if (deviceId && deviceId !== '__default' && typeof relayAudioEl.setSinkId === 'function') {
        try {
          await relayAudioEl.setSinkId(deviceId);
        } catch (err) {
          setError(`setSinkId failed: ${err.message}`);
        }
      }

      try { await relayAudioEl.play(); } catch (_) {
        relayAudioEl.muted = true;
        await relayAudioEl.play().catch(() => {});
        relayAudioEl.muted = false;
      }

      setState('converting');
      document.getElementById('relay-latency').textContent = `${Math.round(performance.now() - startedAt)} ms`;
    });

    relayRoom.on(RoomEvent.TrackUnsubscribed, (track) => {
      track.detach();
    });

    setState('connecting');
    await relayRoom.connect(serverUrl, token);
    await relayRoom.startAudio();

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { noiseSuppression: false, autoGainControl: false, echoCancellation: true },
    });
    relayMicTrack = await relayRoom.localParticipant.publishTrack(
      stream.getAudioTracks()[0],
      { name: 'microphone', source: Track.Source.Microphone },
    );

    startMeters();
    startedAt = performance.now();

    setState('warming');
    await startBot(roomName, identity);

    setState('ready');
    stopBtn.disabled = false;
  } catch (err) {
    setError(err.message);
    setState('interrupted');
    startBtn.disabled = false;
    if (relayRoom) { await relayRoom.disconnect(); relayRoom = null; }
  }
}

async function stopRelay() {
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
  updateMeter('input', 0);
  updateMeter('output', 0);
  document.getElementById('relay-latency').textContent = '--';
  document.getElementById('bot-identity').textContent = '--';
  setState('stopped');
  document.getElementById('start-relay').disabled = false;
  document.getElementById('stop-relay').disabled = true;
}

function startMeters() {
  if (meterInterval) return;
  meterInterval = setInterval(() => {
    if (relayMicTrack) {
      updateMeter('input', Math.round(relayMicTrack.getAudioLevel() * 100));
    }
  }, 200);
}

function stopMeters() {
  if (meterInterval) { clearInterval(meterInterval); meterInterval = null; }
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
    console.warn('Mic permission denied for device labels:', err);
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

  document.getElementById('start-relay').disabled = false;
}

document.getElementById('warm-gpu').addEventListener('click', warmGpu);
document.getElementById('start-relay').addEventListener('click', startRelay);
document.getElementById('stop-relay').addEventListener('click', stopRelay);

populateOutputDevices();
