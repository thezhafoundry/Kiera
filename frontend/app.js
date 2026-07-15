// Keira Softphone Logic - Frontend JS
const { Room, RoomEvent, Track, TrackEvent } = LivekitClient;

const API_BASE = window.location.origin;

// State management
let currentRoom = null;
let currentRoomName = null;
let activeCallTimer = null;
let callStartTime = null;
let isMuted = false;
let agentIdentity = `agent-${Math.floor(1000 + Math.random() * 9000)}`;
let localAudioTrack = null;
let wsConn = null;
let controlToken = null; // kept in memory only; never persisted in localStorage

function getControlToken() {
    if (controlToken) return controlToken;
    const entered = window.prompt('Enter the Keira control token:');
    if (!entered || !entered.trim()) {
        throw new Error('A Keira control token is required');
    }
    controlToken = entered.trim();
    return controlToken;
}

async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('Authorization', `Bearer ${getControlToken()}`);
    if (!wsConn) initWebSocket();
    return fetch(url, { ...options, headers });
}

document.addEventListener('DOMContentLoaded', () => {
    setupEventListeners();
    initWebSocket();
});

function setupEventListeners() {
    // Deploy GPU Button
    document.getElementById('btn-deploy').addEventListener('click', deployGPU);

    // Warmup / Start Shift Button
    document.getElementById('btn-warmup').addEventListener('click', startShift);

    // Manual dialer
    document.getElementById('btn-dial').addEventListener('click', dialManualNumber);
    document.getElementById('dial-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') dialManualNumber();
    });

    // Call acceptance / rejection bu  ttons
    document.getElementById('btn-incoming-accept').addEventListener('click', acceptIncomingCall);
    document.getElementById('btn-incoming-reject').addEventListener('click', rejectIncomingCall);

    // Active call console buttons
    document.getElementById('btn-mute-mic').addEventListener('click', toggleMute);
    document.getElementById('btn-end-call').addEventListener('click', endActiveCall);
}

function selectedAgentGender() {
    return document.getElementById('agent-gender')?.value || 'male';
}


// Initialize WebSockets for Incoming Call Webhook Broadcasts
function initWebSocket() {
    if (wsConn || !controlToken) return;
    const wsProto = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
    const wsUrl = `${wsProto}${window.location.host}/api/call/ws`;
    
    wsConn = new WebSocket(wsUrl, [`keira-auth.${controlToken}`]);

    wsConn.onopen = () => {
        console.log("[WebSocket] Connection established");
        // Keep-alive ping
        setInterval(() => {
            if (wsConn.readyState === WebSocket.OPEN) {
                wsConn.send("ping");
            }
        }, 30000);
    };

    wsConn.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            if (data.event === "incoming_call") {
                showIncomingCallPopup(data.callerId, data.roomName);
            } else if (data.event === "call_ended") {
                if (currentRoomName === data.roomName) {
                    console.log("[WebSocket] Call ended by remote side");
                    resetCallUI();
                    leaveRoom();
                }
            }
        } catch (e) {
            // Ignore non-json (pong messages)
        }
    };

    wsConn.onclose = () => {
        console.log("[WebSocket] Disconnected. Reconnecting...");
        setTimeout(initWebSocket, 5000);
    };
}

// Start Shift - Warmed GPU
async function startShift() {
    const warmupBtn = document.getElementById('btn-warmup');
    const gpuIndicator = document.getElementById('gpu-status-indicator');
    const gpuVal = document.getElementById('val-gpu-status');
    const shiftVal = document.getElementById('val-shift-status');

    warmupBtn.disabled = true;
    gpuIndicator.className = 'status-indicator warming';
    gpuVal.innerText = 'Starting GPU...';

    // Animate status while waiting (GPU can take up to 6 minutes on cold start)
    let dots = 0;
    const loadingInterval = setInterval(() => {
        dots = (dots + 1) % 4;
        gpuVal.innerText = 'Starting GPU' + '.'.repeat(dots);
    }, 800);

    try {
        // Use AbortController with a 7-minute timeout (server retries for up to 6 min)
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 7 * 60 * 1000);

        const resp = await apiFetch(`${API_BASE}/api/warmup`, {
            method: 'POST',
            signal: controller.signal
        });
        clearTimeout(timeoutId);
        clearInterval(loadingInterval);

        const data = await resp.json();
        
        if (data.status === 'success') {
            gpuIndicator.className = 'status-indicator online';
            gpuVal.innerText = 'Warm (Ready)';
            shiftVal.innerText = 'Warmup Complete';
            shiftVal.className = 'status-value online-text';
            warmupBtn.innerText = 'Warmup Complete';
        } else {
            gpuIndicator.className = 'status-indicator error';
            gpuVal.innerText = 'Cold / Error — try again';
            warmupBtn.disabled = false;
            warmupBtn.innerText = 'Start Shift';
        }
    } catch (e) {
        clearInterval(loadingInterval);
        gpuIndicator.className = 'status-indicator error';
        if (e.name === 'AbortError') {
            gpuVal.innerText = 'Timed out — try again';
        } else {
            gpuVal.innerText = 'Error connecting';
        }
        warmupBtn.disabled = false;
        warmupBtn.innerText = 'Start Shift';
    }
}


// Deploy GPU to Modal from Render
async function deployGPU() {
    const deployBtn = document.getElementById('btn-deploy');
    const gpuIndicator = document.getElementById('gpu-status-indicator');
    const gpuVal = document.getElementById('val-gpu-status');

    deployBtn.disabled = true;
    deployBtn.innerText = 'Deploying...';
    gpuIndicator.className = 'status-indicator warming';
    gpuVal.innerText = 'Deploying in cloud...';

    try {
        const resp = await apiFetch(`${API_BASE}/api/deploy`, { method: 'POST' });
        const data = await resp.json();
        
        if (data.status === 'success') {
            gpuIndicator.className = 'status-indicator offline';
            gpuVal.innerText = 'Cold (Deployed)';
            deployBtn.innerText = 'Redeploy GPU';
            deployBtn.disabled = false;
            alert("Modal GPU deployed successfully! Click 'Start Shift' to warm it up.");
        } else {
            gpuIndicator.className = 'status-indicator error';
            gpuVal.innerText = 'Deploy Failed';
            deployBtn.innerText = 'Deploy GPU';
            deployBtn.disabled = false;
            alert("Deployment failed: " + data.message + "\n\nOutput:\n" + (data.output || ""));
        }
    } catch (e) {
        gpuIndicator.className = 'status-indicator error';
        gpuVal.innerText = 'Connection Error';
        deployBtn.innerText = 'Deploy GPU';
        deployBtn.disabled = false;
        alert("Error connecting to backend for deployment: " + e.message);
    }
}


// Show incoming call popup
let incomingRoomName = null;
function showIncomingCallPopup(callerId, roomName) {
    incomingRoomName = roomName;
    document.getElementById('incoming-caller-id').innerText = callerId;
    document.getElementById('incoming-call-overlay').style.display = 'flex';
}

function hideIncomingCallPopup() {
    document.getElementById('incoming-call-overlay').style.display = 'none';
    incomingRoomName = null;
}

// Accept inbound call
async function acceptIncomingCall() {
    if (!incomingRoomName) return;
    const roomName = incomingRoomName;
    hideIncomingCallPopup();

    // Set UI to active call state
    document.getElementById('active-lead-name').innerText = "Inbound Lead";
    document.getElementById('active-lead-number').innerText = "Twilio SIP Line";
    setupCallUI();

    try {
        const resp = await apiFetch(`${API_BASE}/api/call/accept`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ roomName, agentIdentity, agentGender: selectedAgentGender() })
        });
        
        if (!resp.ok) throw new Error("Accept endpoint failed");
        
        const data = await resp.json();
        await connectToRoom(data.serverUrl, data.token, roomName);
    } catch (e) {
        alert("Failed to accept call: " + e.message);
        resetCallUI();
    }
}

// Reject inbound call
async function rejectIncomingCall() {
    if (!incomingRoomName) return;
    const roomName = incomingRoomName;
    hideIncomingCallPopup();

    try {
        await apiFetch(`${API_BASE}/api/call/end`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ roomName })
        });
    } catch (e) {
        console.error("Error rejecting call", e);
    }
}

// Start outbound call
async function startOutboundCall(phone, name) {
    // Set UI to active call state
    document.getElementById('active-lead-name').innerText = name;
    document.getElementById('active-lead-number').innerText = phone;
    setupCallUI();

    let preparedRoom = null;
    try {
        const resp = await apiFetch(`${API_BASE}/api/call/outbound`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ phoneNumber: phone, agentIdentity, agentGender: selectedAgentGender() })
        });
        
        if (!resp.ok) {
            let detail = `Server error ${resp.status}`;
            try { const err = await resp.json(); detail = err.detail || JSON.stringify(err); } catch {}
            throw new Error(detail);
        }

        const data = await resp.json();
        preparedRoom = data.roomName;
        await connectToRoom(data.serverUrl, data.token, data.roomName);

        const dialResp = await apiFetch(`${API_BASE}/api/call/outbound/dial`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ roomName: data.roomName, phoneNumber: phone })
        });
        if (!dialResp.ok) {
            let detail = `Server error ${dialResp.status}`;
            try { const err = await dialResp.json(); detail = err.detail || JSON.stringify(err); } catch {}
            throw new Error(detail);
        }
    } catch (e) {
        alert("Outbound call failed:\n\n" + e.message);
        const failedRoom = preparedRoom || currentRoomName;
        if (failedRoom && controlToken) {
            try {
                await apiFetch(`${API_BASE}/api/call/end`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ roomName: failedRoom })
                });
            } catch (cleanupError) {
                console.error("Failed to clean up outbound room:", cleanupError);
            }
        }
        leaveRoom();
        resetCallUI();
    }
}

// Normalize manual dial input to E.164 (+91 default country code).
// Returns null when the input can't be made into a plausible number.
function normalizeDialNumber(raw) {
    const cleaned = (raw || '').replace(/[\s\-\.\(\)]/g, '');
    if (/^\+\d{8,15}$/.test(cleaned)) return cleaned;
    if (/^\d{10}$/.test(cleaned)) return '+91' + cleaned;
    if (/^91\d{10}$/.test(cleaned)) return '+' + cleaned;
    return null;
}

function dialManualNumber() {
    const input = document.getElementById('dial-input');
    const errorEl = document.getElementById('dial-error');
    const number = normalizeDialNumber(input.value);
    if (!number) {
        errorEl.innerText = 'Enter a 10-digit number or full +country format';
        errorEl.style.display = 'block';
        return;
    }
    errorEl.style.display = 'none';
    startOutboundCall(number, number);
}

// Connect to LiveKit room
async function connectToRoom(serverUrl, token, roomName) {
    currentRoomName = roomName;

    try {
        currentRoom = new Room({
            adaptiveStream: true,
            dynacast: true,
        });

        // Set up room events
        setupRoomEvents(currentRoom);

        await currentRoom.connect(serverUrl, token);
        console.log(`[LiveKit] Connected to room: ${roomName}`);

        // CRITICAL: Unlock audio playback after user interaction (browser autoplay policy)
        // Must be called after connect and after a user gesture (button click)
        await currentRoom.startAudio();
        console.log('[LiveKit] Audio context started — playback unlocked');

        // Set Bot Status to Online as bot auto-joins
        updateBotUI(true);

        // Publish agent mic
        await publishAgentMicrophone(currentRoom, roomName);
        
        startTimer();
    } catch (e) {
        console.error("LiveKit connection error:", e);
        alert("LiveKit connection failed: " + e.message);
        leaveRoom();
        resetCallUI();
        throw e;
    }
}

// Setup LiveKit room event listeners
function setupRoomEvents(room) {
    room.on(RoomEvent.TrackSubscribed, (track, publication, participant) => {
        console.log(`[LiveKit] Track subscribed: ${track.sid} from ${participant.identity}`);

        // Skip the bot's converted audio — it's for the lead's phone, not the agent's laptop.
        // Playing it on the laptop causes feedback and the agent doesn't need to hear their own voice.
        if (participant.identity.startsWith('voice-converter-bot')) {
            return;
        }

        const container = document.getElementById('audio-outputs-container');
        const audioElement = track.attach();
        audioElement.volume = 1.0;
        container.appendChild(audioElement);

        audioElement.play().then(() => {
            console.log(`[LiveKit] ✅ Playing audio from: ${participant.identity}`);
        }).catch(e => {
            console.error(`[LiveKit] ❌ Audio play() blocked by browser:`, e);
            if (currentRoom) currentRoom.startAudio();
        });
    });

    room.on(RoomEvent.TrackUnsubscribed, (track, publication, participant) => {
        console.log(`[LiveKit] Track unsubscribed: ${track.sid}`);
        track.detach();
    });

    room.on(RoomEvent.ParticipantConnected, (participant) => {
        if (participant.identity.startsWith('voice-converter-bot')) {
            updateBotUI(true);
        }
    });

    room.on(RoomEvent.ParticipantDisconnected, (participant) => {
        if (participant.identity.startsWith('voice-converter-bot')) {
            updateBotUI(false);
        }
    });

    // Real-Time Data Channel metrics listener for RVC latency
    room.on(RoomEvent.DataReceived, (payload, participant) => {
        try {
            const decoder = new TextDecoder();
            const text = decoder.decode(payload);
            const data = JSON.parse(text);

            if (data.pipeline_latency_ms !== undefined) {
                const latencyVal = document.getElementById('latency-val-badge');
                const progressBar = document.getElementById('latency-progress-fill');
                const failsafeBadge = document.getElementById('failsafe-active-badge');
                
                const latency = data.pipeline_latency_ms;
                latencyVal.innerText = `${latency.toFixed(0)} ms`;

                // Update latency progress bar (e.g. mapping 0-1000ms to 0-100%)
                const percentage = Math.min(100, (latency / 1000) * 100);
                progressBar.style.width = `${percentage}%`;

                if (latency > 350) {
                    progressBar.style.backgroundColor = '#f59e0b'; // orange for high latency
                } else {
                    progressBar.style.backgroundColor = '#6366f1'; // indigo for normal
                }

                if (data.is_fallback) {
                    failsafeBadge.style.display = 'inline-block';
                    progressBar.style.backgroundColor = '#ef4444'; // red for fallback/failsafe
                } else {
                    failsafeBadge.style.display = 'none';
                }
            }
        } catch (e) {
            console.error("Error parsing data channel message:", e);
        }
    });
}

// Publish agent mic and restrict subscription permissions
async function publishAgentMicrophone(room, roomName) {
    try {
        // Restrict subscription BEFORE publishing: this is a participant-level setting that
        // applies to all tracks this participant publishes from now on, so setting it first
        // closes the window where the SIP/phone participant could auto-subscribe to the raw
        // mic track before the restriction takes effect (e.g. Twilio bridging in while this
        // browser is still awaiting getUserMedia/publishTrack).
        const botIdentity = `voice-converter-bot-${roomName}`;
        await room.localParticipant.setTrackSubscriptionPermissions(false, [
            {
                participantIdentity: botIdentity,
                allowAll: true
            }
        ]);
        console.log(`[LiveKit] Selective subscription set: Only ${botIdentity} can subscribe to agent raw track`);

        // Capture RAW mic audio: the browser's default noiseSuppression and
        // autoGainControl aggressively strip the high-frequency detail (sibilance,
        // consonants) and flatten the dynamics that the RVC model needs to produce a
        // clear, on-identity brand voice — measured 2026-07-08 as a major cause of the
        // "muffled" output. echoCancellation stays ON so the lead's voice coming out of
        // the agent's speakers isn't picked up and echoed back; if the agent wears
        // headphones it can be disabled too for maximum fidelity.
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: {
                noiseSuppression: false,
                autoGainControl: false,
                echoCancellation: true,
            },
        });
        const audioStreamTrack = stream.getAudioTracks()[0];
        localAudioTrack = await room.localParticipant.publishTrack(audioStreamTrack, {
            name: "microphone",
            source: Track.Source.Microphone
        });

        console.log("[LiveKit] Microphone track published");
    } catch (e) {
        console.error("Error publishing microphone:", e);
        alert("Could not access microphone: " + e.message);
    }
}

// Mute / Unmute microphone
function toggleMute() {
    if (!localAudioTrack) return;
    
    isMuted = !isMuted;
    localAudioTrack.setEnabled(!isMuted);

    const muteBtn = document.getElementById('btn-mute-mic');
    const muteText = document.getElementById('btn-mute-text');

    if (isMuted) {
        muteBtn.className = 'btn btn-outline-warning muted';
        muteText.innerText = 'Unmute Mic';
    } else {
        muteBtn.className = 'btn btn-outline-warning';
        muteText.innerText = 'Mute Mic';
    }
}

// End the active call
async function endActiveCall() {
    if (!currentRoomName) return;
    const roomName = currentRoomName;

    resetCallUI();
    leaveRoom();

    try {
        await apiFetch(`${API_BASE}/api/call/end`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ roomName })
        });
    } catch (e) {
        console.error("Error ending call on server:", e);
    }
}

function leaveRoom() {
    if (currentRoom) {
        currentRoom.disconnect();
        currentRoom = null;
    }
    currentRoomName = null;
    localAudioTrack = null;
    updateBotUI(false);
}

// UI State Toggles
function setupCallUI() {
    document.getElementById('console-idle-view').style.display = 'none';
    document.getElementById('console-active-view').style.display = 'block';
    
    // Disable dial buttons during a call
    document.querySelectorAll('.call-action-btn').forEach(btn => btn.disabled = true);
    
    isMuted = false;
    const muteBtn = document.getElementById('btn-mute-mic');
    const muteText = document.getElementById('btn-mute-text');
    muteBtn.className = 'btn btn-outline-warning';
    muteText.innerText = 'Mute Mic';
}

function resetCallUI() {
    stopTimer();
    document.getElementById('call-duration-timer').innerText = "00:00";
    document.getElementById('console-active-view').style.display = 'none';
    document.getElementById('console-idle-view').style.display = 'block';
    
    // Enable dial buttons
    document.querySelectorAll('.call-action-btn').forEach(btn => btn.disabled = false);

    // Reset metrics
    document.getElementById('latency-val-badge').innerText = "-- ms";
    document.getElementById('latency-progress-fill').style.width = "0%";
    document.getElementById('failsafe-active-badge').style.display = "none";
}

function updateBotUI(online) {
    const indicator = document.getElementById('bot-status-indicator');
    const textVal = document.getElementById('val-bot-status');

    if (online) {
        indicator.className = 'status-indicator online';
        textVal.innerText = 'Connected';
    } else {
        indicator.className = 'status-indicator';
        textVal.innerText = 'Offline';
    }
}

// Active Call Timer helpers
function startTimer() {
    stopTimer();
    callStartTime = Date.now();
    activeCallTimer = setInterval(updateTimerUI, 1000);
}

function stopTimer() {
    if (activeCallTimer) {
        clearInterval(activeCallTimer);
        activeCallTimer = null;
    }
}

function updateTimerUI() {
    if (!callStartTime) return;
    const elapsedSecs = Math.floor((Date.now() - callStartTime) / 1000);
    const mins = Math.floor(elapsedSecs / 60).toString().padStart(2, '0');
    const secs = (elapsedSecs % 60).toString().padStart(2, '0');
    document.getElementById('call-duration-timer').innerText = `${mins}:${secs}`;
}
