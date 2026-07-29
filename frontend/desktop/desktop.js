const CAPTURE_SAMPLE_RATE = 48_000;
const INPUT_SAMPLE_RATE = 16_000;
const OUTPUT_SAMPLE_RATE = 48_000;

const defaultWebSocketFactory = (url, protocols) => new WebSocket(url, protocols);
const defaultAudioContextFactory = (options) => new AudioContext(options);

export const isCableInputSink = (label = '') => /cable\s+input/i.test(label);
export const isCableOutputInput = (label = '') => /cable\s+output/i.test(label);
export const isVirtualLoopbackInput = (label = '') =>
  /cable\s+(?:input|output)|blackhole|loopback/i.test(label);
export const isApprovedVirtualOutput = (label = '') =>
  isCableInputSink(label) || /blackhole|loopback/i.test(label);
const audioContextSupportsSinkId = () =>
  typeof AudioContext !== 'undefined'
  && typeof AudioContext.prototype.setSinkId === 'function';

const rmsPcm16 = (pcm) => {
  if (!(pcm instanceof ArrayBuffer) || pcm.byteLength % 2 !== 0) {
    return 0;
  }
  const view = new DataView(pcm);
  let energy = 0;
  for (let index = 0; index < pcm.byteLength; index += 2) {
    const sample = view.getInt16(index, true) / 32768;
    energy += sample * sample;
  }
  return Math.sqrt(energy / (pcm.byteLength / 2));
};

/** Browser client for the fail-closed desktop voice conversion relay. */
export class DesktopAudioClient {
  constructor({
    createWebSocket = defaultWebSocketFactory,
    createAudioContext = defaultAudioContextFactory,
    mediaDevices = globalThis.navigator?.mediaDevices,
    location = globalThis.location,
    AudioWorkletNodeClass = globalThis.AudioWorkletNode,
  } = {}) {
    this.createWebSocket = createWebSocket;
    this.createAudioContext = createAudioContext;
    this.mediaDevices = mediaDevices;
    this.location = location;
    this.AudioWorkletNodeClass = AudioWorkletNodeClass;
    this.statusCallbacks = new Set();
    this.meterCallbacks = new Set();
    this.meters = { input: 0, output: 0, bufferMs: 0 };
    this.socket = null;
    this.context = null;
    this.stream = null;
    this.source = null;
    this.captureNode = null;
    this.playoutNode = null;
    this.stopping = false;
    this.relayReady = false;
    // Diagnostic capture of converted frames exactly as they arrive off the
    // socket, before the worklet transfers (and neuters) the buffer. Lets a
    // silent test be split into "audio never arrived" vs "audio arrived but
    // did not play" without guessing at device routing.
    this.recordOutput = false;
    this.recordedChunks = [];
    this.recordedBytes = 0;
  }

  onStatus(callback) {
    this.statusCallbacks.add(callback);
  }

  onMeters(callback) {
    this.meterCallbacks.add(callback);
  }

  emitStatus(status) {
    for (const callback of this.statusCallbacks) {
      callback(status);
    }
  }

  emitMeters(meters) {
    Object.assign(this.meters, meters);
    for (const callback of this.meterCallbacks) {
      callback({ ...this.meters });
    }
  }

  async start({ inputDeviceId, outputDeviceId, ticket }) {
    if (!ticket) {
      throw new TypeError('A desktop session ticket is required');
    }
    if (!this.mediaDevices?.getUserMedia) {
      throw new Error('Microphone capture is unavailable in this browser');
    }
    if (!this.AudioWorkletNodeClass) {
      throw new Error('AudioWorklet is unavailable in this browser');
    }
    if (this.context || this.socket) {
      await this.stop();
    }

    this.stopping = false;
    this.relayReady = false;
    this.emitStatus({ type: 'starting' });
    try {
      this.context = this.createAudioContext({ sampleRate: CAPTURE_SAMPLE_RATE });
      if (this.context.sampleRate !== CAPTURE_SAMPLE_RATE) {
        throw new Error(`AudioContext must run at ${CAPTURE_SAMPLE_RATE} Hz; got ${this.context.sampleRate} Hz`);
      }
      if (outputDeviceId) {
        if (typeof this.context.setSinkId !== 'function') {
          throw new Error('AudioContext.setSinkId is unavailable; use current Chrome or Edge on Windows');
        }
        await this.context.setSinkId(outputDeviceId);
      }

      await Promise.all([
        this.context.audioWorklet.addModule('./capture-worklet.js', { type: 'module' }),
        this.context.audioWorklet.addModule('./playout-worklet.js', { type: 'module' }),
      ]);
      this.captureNode = new this.AudioWorkletNodeClass(this.context, 'keira-capture');
      this.playoutNode = new this.AudioWorkletNodeClass(this.context, 'keira-playout');
      this.playoutNode.connect(this.context.destination);
      this.bindWorklets();

      this.socket = await this.openSocket(ticket);
      this.stream = await this.mediaDevices.getUserMedia({
        audio: inputDeviceId ? { deviceId: { exact: inputDeviceId } } : true,
      });
      this.source = this.context.createMediaStreamSource(this.stream);
      this.source.connect(this.captureNode);
      await this.context.resume();
      this.emitStatus({ type: 'connected' });
    } catch (error) {
      this.emitStatus({ type: 'error', message: error.message });
      await this.release({ closeSocket: true });
      throw error;
    }
  }

  async stop() {
    if (!this.context && !this.socket && !this.stream) {
      return;
    }
    this.stopping = true;
    await this.release({ closeSocket: true });
    this.emitStatus({ type: 'stopped' });
  }

  bindWorklets() {
    this.captureNode.port.onmessage = ({ data }) => {
      if (data?.type === 'frame') {
        if (this.relayReady && data.pcm instanceof ArrayBuffer && data.pcm.byteLength === 640 && this.socket?.readyState === 1) {
          this.socket.send(data.pcm);
        }
      } else if (data?.type === 'meter') {
        this.emitMeters({ input: data.input, output: 0, bufferMs: 0 });
      } else if (data?.type === 'error') {
        this.fail(data.message);
      }
    };
    this.playoutNode.port.onmessage = ({ data }) => {
      if (data?.type === 'meter') {
        this.emitMeters({
          input: 0,
          output: data.output,
          bufferMs: data.bufferMs,
          oldestDropCount: data.oldestDropCount,
          underrunCount: data.underrunCount,
        });
      }
    };
  }

  openSocket(ticket) {
    const scheme = this.location?.protocol === 'http:' ? 'ws' : 'wss';
    const url = `${scheme}://${this.location.host}/api/desktop/audio`;
    const socket = this.createWebSocket(url, [`keira-desktop.${ticket}`]);
    this.socket = socket;
    socket.binaryType = 'arraybuffer';

    return new Promise((resolve, reject) => {
      let connected = false;
      socket.onopen = () => {
        try {
          socket.send(JSON.stringify({
            type: 'config',
            sample_rate_in: INPUT_SAMPLE_RATE,
            sample_rate_out: OUTPUT_SAMPLE_RATE,
            frame_ms: 20,
          }));
          connected = true;
          resolve(socket);
        } catch (error) {
          reject(error);
        }
      };
      socket.onmessage = (event) => this.handleSocketMessage(event.data);
      socket.onerror = () => {
        const error = new Error('Desktop audio relay connection failed');
        if (!connected) {
          reject(error);
          return;
        }
        this.fail(error.message);
      };
      socket.onclose = (event) => {
        if (!connected) {
          reject(new Error(`Desktop audio relay closed (${event.code || 'unknown'})`));
          return;
        }
        if (!this.stopping) {
          this.fail(`Desktop audio relay closed (${event.code || 'unknown'})`);
        }
      };
    });
  }

  handleSocketMessage(message) {
    if (message instanceof ArrayBuffer) {
      const output = rmsPcm16(message);
      // Copy BEFORE postMessage: the transfer list neuters `message`, so a
      // capture taken afterward would read an empty buffer.
      if (this.recordOutput) {
        this.recordedChunks.push(new Uint8Array(message.slice(0)));
        this.recordedBytes += message.byteLength;
      }
      this.playoutNode?.port.postMessage({ type: 'audio', pcm: message }, [message]);
      this.emitMeters({ input: 0, output, bufferMs: 0 });
      return;
    }
    if (typeof message !== 'string') {
      return;
    }
    try {
      const status = JSON.parse(message);
      if (status.type === 'ready') {
        this.relayReady = true;
      }
      if (status.type === 'stopped') {
        // Server-side accounting for this session, used to locate audio loss:
        // what the relay wrote vs. what this client actually captured.
        this.serverSentBytes = status.playout_sent_bytes ?? null;
        this.serverDropBytes = status.playout_drop_bytes ?? null;
      }
      this.emitStatus(status);
      if (status.type === 'error') {
        this.fail(status.message || 'Desktop audio relay error');
      }
    } catch {
      this.fail('Desktop audio relay sent invalid status data');
    }
  }

  /** Wrap the captured PCM in a WAV container, or null if nothing arrived. */
  buildRecordingBlob() {
    if (this.recordedBytes === 0) {
      return null;
    }
    const pcm = new Uint8Array(this.recordedBytes);
    let offset = 0;
    for (const chunk of this.recordedChunks) {
      pcm.set(chunk, offset);
      offset += chunk.byteLength;
    }

    const header = new ArrayBuffer(44);
    const view = new DataView(header);
    const writeAscii = (at, text) => {
      for (let i = 0; i < text.length; i += 1) view.setUint8(at + i, text.charCodeAt(i));
    };
    const byteRate = OUTPUT_SAMPLE_RATE * 2; // mono, 16-bit
    writeAscii(0, 'RIFF');
    view.setUint32(4, 36 + pcm.byteLength, true);
    writeAscii(8, 'WAVE');
    writeAscii(12, 'fmt ');
    view.setUint32(16, 16, true); // PCM chunk size
    view.setUint16(20, 1, true); // format = PCM
    view.setUint16(22, 1, true); // channels
    view.setUint32(24, OUTPUT_SAMPLE_RATE, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, 2, true); // block align
    view.setUint16(34, 16, true); // bits per sample
    writeAscii(36, 'data');
    view.setUint32(40, pcm.byteLength, true);

    return new Blob([header, pcm], { type: 'audio/wav' });
  }

  fail(message) {
    if (this.stopping) {
      return;
    }
    this.stopping = true;
    this.emitStatus({ type: 'interrupted', message });
    void this.release({ closeSocket: true });
  }

  async release({ closeSocket }) {
    const socket = this.socket;
    this.socket = null;
    this.relayReady = false;
    if (this.captureNode?.port) {
      this.captureNode.port.onmessage = null;
    }
    if (this.playoutNode?.port) {
      this.playoutNode.port.onmessage = null;
    }
    this.source?.disconnect();
    this.captureNode?.disconnect();
    this.playoutNode?.disconnect();
    this.stream?.getTracks().forEach((track) => track.stop());
    this.source = null;
    this.captureNode = null;
    this.playoutNode = null;
    this.stream = null;
    const context = this.context;
    this.context = null;
    if (closeSocket && socket && socket.readyState < 2) {
      socket.close();
    }
    if (context?.close) {
      await context.close();
    }
  }
}

// How long to wait for the first converted frame, measured from the relay's
// `ready` handshake. The converter buffers BLOCK_MS+CONTEXT_MS (720ms) before
// its first inference and returns it ~1.2-1.4s later; measured warm, the first
// frame lands ~1.36s after `ready`. TEMPORARY: bumped 10s -> 30s to diagnose
// audio arriving after the test already closed the socket (2026-07-29); revert
// once real first-block latency is confirmed.
const VOICE_TEST_AUDIO_TIMEOUT_MS = 30_000;
// Cap on the `ready` handshake itself. The backend's own fail-closed gate is
// 150s (READINESS_TIMEOUT_SECONDS); a cold Modal container has been measured
// taking 24s+ just to hand back `ready`.
const VOICE_TEST_READY_TIMEOUT_MS = 60_000;

const byId = (id) => document.getElementById(id);
const formatMs = (value) => Number.isFinite(value) ? `${Math.round(value)} ms` : '--';
const formatPercent = (value) => `${Math.round(Math.min(1, Math.max(0, value || 0)) * 100)}%`;

/** Controls the desktop setup page without persisting its control-plane token. */
export class DesktopSetupPage {
  constructor({ createClient = () => new DesktopAudioClient(), fetchImpl = globalThis.fetch.bind(globalThis) } = {}) {
    this.createClient = createClient;
    this.fetchImpl = fetchImpl;
    this.client = null;
    this.state = 'signed_out';
    this.backendReady = false;
    this.authModeReady = false;
    this.authRequired = true;
    this.lastError = '';
    this.devices = { inputs: [], outputs: [] };
  }

  init() {
    this.tokenInput = byId('control-token');
    this.profileInput = byId('voice-profile');
    this.inputSelect = byId('microphone-device');
    this.outputSelect = byId('output-device');
    this.startButton = byId('start-conversion');
    this.stopButton = byId('stop-conversion');
    this.testButton = byId('voice-test');
    this.warmButton = byId('warm-gpu');
    this.warmResult = byId('warm-gpu-result');
    this.tokenInput.addEventListener('input', () => this.onTokenInput());
    this.profileInput.addEventListener('change', () => this.refreshControls());
    this.inputSelect.addEventListener('change', () => this.validateDevices());
    this.outputSelect.addEventListener('change', () => this.validateDevices());
    this.startButton.addEventListener('click', () => void this.startConversion());
    this.stopButton.addEventListener('click', () => void this.stopConversion());
    this.testButton.addEventListener('click', () => void this.runVoiceTest());
    this.warmButton.addEventListener('click', () => void this.warmGpu());
    this.setState('signed_out');
    void this.loadAuthMode();
    void this.enumerateDevices();
  }

  token() {
    return this.tokenInput?.value.trim() || '';
  }

  async loadAuthMode() {
    try {
      const response = await this.fetchImpl('/api/desktop/auth-mode');
      const mode = await response.json();
      this.authRequired = mode.auth_required !== false;
      this.authModeReady = true;
      const tokenLabel = this.tokenInput?.closest('label');
      if (tokenLabel) tokenLabel.hidden = !this.authRequired;
      if (!this.authRequired && this.tokenInput) this.tokenInput.value = '';
      if (!this.authRequired && !this.client) this.setState('stopped');
    } catch {
      this.authRequired = true;
      this.authModeReady = true;
    }
    this.refreshControls();
  }

  onTokenInput() {
    if (!this.client) this.setState(this.canUseDesktopSession() ? 'stopped' : 'signed_out');
    this.refreshControls();
  }

  canUseDesktopSession() {
    return this.authModeReady && (!this.authRequired || Boolean(this.token()));
  }

  canRunVoiceTest() {
    const input = this.selectedInput();
    return this.canUseDesktopSession() && input && !isVirtualLoopbackInput(input.label);
  }

  setState(state, message = '') {
    this.state = state;
    byId('connection-state').textContent = state.replace('_', ' ');
    byId('connection-state').dataset.state = state;
    if (message) this.setError(message);
    this.refreshControls();
  }

  setError(message = '') {
    this.lastError = message;
    const element = byId('page-error');
    element.textContent = message;
    element.hidden = !message;
  }

  setNotice(message = '') {
    const element = byId('device-warning');
    element.textContent = message;
    element.hidden = !message;
  }

  async enumerateDevices() {
    if (!navigator.mediaDevices?.getUserMedia || !navigator.mediaDevices?.enumerateDevices) {
      this.setError('This browser cannot enumerate microphone and output devices.');
      return;
    }
    try {
      const permissionStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      permissionStream.getTracks().forEach((track) => track.stop());
      const devices = await navigator.mediaDevices.enumerateDevices();
      this.devices.inputs = devices.filter((device) => device.kind === 'audioinput');
      this.devices.outputs = devices.filter((device) => device.kind === 'audiooutput');
      this.populateDevices(this.inputSelect, this.devices.inputs, 'Select a microphone');
      this.populateDevices(this.outputSelect, this.devices.outputs, 'Select virtual output');
      this.validateDevices();
    } catch (error) {
      this.setError(`Microphone permission is required to select devices: ${error.message}`);
    }
  }

  populateDevices(select, devices, placeholder) {
    const selected = select.value;
    select.replaceChildren(new Option(placeholder, ''));
    devices.forEach((device, index) => {
      select.add(new Option(device.label || `${device.kind} ${index + 1}`, device.deviceId));
    });
    if (devices.some((device) => device.deviceId === selected)) select.value = selected;
  }

  selectedInput() {
    return this.devices.inputs.find((device) => device.deviceId === this.inputSelect.value);
  }

  selectedOutput() {
    return this.devices.outputs.find((device) => device.deviceId === this.outputSelect.value);
  }

  validateDevices() {
    const input = this.selectedInput();
    const output = this.selectedOutput();
    if (input && isVirtualLoopbackInput(input.label)) {
      this.setNotice('A virtual loopback device is selected as the input. Choose your physical microphone.');
    } else if (output && !isApprovedVirtualOutput(output.label)) {
      this.setNotice('Converted output must be VB-CABLE CABLE Input, BlackHole, or Loopback.');
    } else if (output && !audioContextSupportsSinkId()) {
      this.setNotice('AudioContext.setSinkId is required. Use a current Chrome or Edge build.');
    } else {
      this.setNotice('');
    }
    this.refreshControls();
  }

  canStart() {
    const input = this.selectedInput();
    const output = this.selectedOutput();
    return Boolean(
      this.canUseDesktopSession()
      && input && !isVirtualLoopbackInput(input.label)
      && output && isApprovedVirtualOutput(output.label)
      && audioContextSupportsSinkId()
      && !this.client
      && !['starting', 'warming', 'converting'].includes(this.state)
    );
  }

  refreshControls() {
    if (!this.startButton) return;
    this.startButton.disabled = !this.canStart();
    this.stopButton.disabled = !this.client;
    this.testButton.disabled = !this.canRunVoiceTest()
      || ['starting', 'warming', 'converting'].includes(this.state);
  }

  async requestTicket() {
    const headers = { 'Content-Type': 'application/json' };
    if (this.token()) headers.Authorization = `Bearer ${this.token()}`;
    const response = await this.fetchImpl('/api/desktop/session', {
      method: 'POST',
      headers,
      body: JSON.stringify({ profile: this.profileInput.value }),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.detail || `Session request failed (${response.status})`);
    }
    const session = await response.json();
    if (!session.ticket || typeof session.ticket !== 'string') throw new Error('Backend returned an invalid desktop ticket');
    return session.ticket;
  }

  bindClient(client, { test = false, startedAt = 0 } = {}) {
    client.onStatus((status) => {
      if (status.type === 'connected') {
        this.setState(this.backendReady ? 'converting' : 'warming');
      } else if (status.type === 'ready') {
        this.backendReady = true;
        if (!test) this.setError('');
        this.setState('ready');
      } else if (status.type === 'interrupted' || status.type === 'error') {
        this.backendReady = false;
        this.setState('interrupted', status.message || 'Desktop audio relay interrupted');
      } else if (status.type === 'stats') {
        this.updateMeters(status);
      } else if (status.type === 'stopped' && !test) {
        this.backendReady = false;
        this.setState('stopped');
      }
    });
    client.onMeters((meters) => {
      this.updateMeters(meters);
      if (test && meters.output > 0) {
        const elapsed = performance.now() - startedAt;
        const latency = formatMs(elapsed);
        byId('voice-test-result').textContent = `Converted audio received in ${latency}`;
        byId('voice-test-latency').textContent = latency;
      }
    });
  }

  updateMeters(meters) {
    if ('input' in meters) {
      byId('input-meter-fill').style.width = formatPercent(meters.input);
      byId('input-meter-value').textContent = formatPercent(meters.input);
    }
    if ('output' in meters) {
      byId('output-meter-fill').style.width = formatPercent(meters.output);
      byId('output-meter-value').textContent = formatPercent(meters.output);
    }
    if ('bufferMs' in meters) byId('playout-buffer').textContent = formatMs(meters.bufferMs);
    if ('input_drop_count' in meters) byId('input-drops').textContent = String(meters.input_drop_count);
    if ('oldestDropCount' in meters) byId('playout-drops').textContent = String(meters.oldestDropCount);
    if ('reconnect_count' in meters) byId('reconnect-count').textContent = String(meters.reconnect_count);
  }

  async startConversion() {
    if (!this.canStart()) return;
    this.backendReady = false;
    this.setState('starting');
    try {
      const ticket = await this.requestTicket();
      this.setState('warming');
      const client = this.createClient();
      this.client = client;
      this.bindClient(client);
      await client.start({ inputDeviceId: this.inputSelect.value, outputDeviceId: this.outputSelect.value, ticket });
    } catch (error) {
      this.backendReady = false;
      this.client = null;
      this.setState('interrupted', error.message);
    }
  }

  async stopConversion() {
    const client = this.client;
    this.client = null;
    if (client) await client.stop();
    this.backendReady = false;
    this.updateMeters({ input: 0, output: 0, bufferMs: 0, input_drop_count: 0, oldestDropCount: 0, reconnect_count: 0 });
    this.setState(this.canUseDesktopSession() ? 'stopped' : 'signed_out');
  }

  async warmGpu() {
    this.warmButton.disabled = true;
    this.warmResult.textContent = 'Warming GPU (cold start can take a few minutes)…';
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 7 * 60 * 1000);
    try {
      const response = await this.fetchImpl('/api/warmup', {
        method: 'POST',
        signal: controller.signal,
      });
      const data = await response.json().catch(() => ({}));
      this.warmResult.textContent = data.status === 'success'
        ? 'GPU warm and ready.'
        : `GPU warmup failed: ${data.message || `HTTP ${response.status}`}`;
    } catch (error) {
      this.warmResult.textContent = error.name === 'AbortError'
        ? 'GPU warmup timed out. Try again.'
        : `GPU warmup failed: ${error.message}`;
    } finally {
      clearTimeout(timeoutId);
      this.warmButton.disabled = false;
    }
  }

  async runVoiceTest() {
    if (!this.canRunVoiceTest()) return;
    this.testButton.disabled = true;
    byId('voice-test-result').textContent = 'Recording a short converted voice test…';
    const client = this.createClient();
    const startedAt = performance.now();
    let receivedAudio = false;
    let resolveFirstAudio;
    let sawPlayoutBuffer = false;
    let resolvePlayoutDrained;
    const firstAudio = new Promise((resolve) => { resolveFirstAudio = resolve; });
    const playoutDrained = new Promise((resolve) => { resolvePlayoutDrained = resolve; });
    let relayReady = false;
    let resolveRelayReady;
    const relayReadyPromise = new Promise((resolve) => { resolveRelayReady = resolve; });
    this.bindClient(client, { test: true, startedAt });
    client.onStatus((status) => {
      if (status.type === 'ready') {
        relayReady = true;
        resolveRelayReady();
      } else if (status.type === 'interrupted' || status.type === 'error') {
        resolveRelayReady();
      }
    });
    client.onMeters((meters) => {
      if (meters.output > 0) {
        receivedAudio = true;
        resolveFirstAudio();
      }
      if (Number.isFinite(meters.bufferMs)) {
        sawPlayoutBuffer ||= meters.bufferMs > 0;
        if (sawPlayoutBuffer && meters.bufferMs <= 20) resolvePlayoutDrained();
      }
    });
    client.recordOutput = true;
    try {
      const ticket = await this.requestTicket();
      await client.start({ inputDeviceId: this.inputSelect.value, ticket });

      // Wait for the relay handshake first. A cold Modal container has been
      // measured taking 24s+ to hand back `ready`; folding that into the audio
      // budget is what made this test tear the socket down mid-conversion.
      byId('voice-test-result').textContent = 'Connecting to the relay…';
      await Promise.race([
        relayReadyPromise,
        new Promise((resolve) => setTimeout(resolve, VOICE_TEST_READY_TIMEOUT_MS)),
      ]);
      if (!relayReady) {
        byId('voice-test-result').textContent = 'Relay never became ready. Warm the GPU and try again.';
        return;
      }

      // Only now start the conversion budget. The converter buffers
      // BLOCK_MS+CONTEXT_MS (720ms) before its first inference and returns it
      // ~1.2-1.4s later, so the first frame lands ~1.36s after `ready`.
      byId('voice-test-result').textContent = 'Speak now, continuously, for a few seconds — waiting for converted audio…';
      await Promise.race([
        firstAudio,
        new Promise((resolve) => setTimeout(resolve, VOICE_TEST_AUDIO_TIMEOUT_MS)),
      ]);
      if (!receivedAudio) {
        byId('voice-test-result').textContent =
          'No converted audio returned. The converter only produces output from continuous '
          + 'voiced input — a pause resets the buffering. Try again speaking without pausing.';
      }
      if (receivedAudio) {
        await new Promise((resolve) => setTimeout(resolve, 150));
        // Cap on waiting for the playout buffer to drain after first audio.
        // A slow/cold backend (confirmed live 2026-07-29: engine=onnx-cuda,
        // not trt) can still be delivering the burst well past a short cap,
        // and client.stop() below tears the socket down unconditionally —
        // cutting playback off mid-stream ("split second, then gone"). Give
        // it real headroom instead of assuming a warm-path drain time.
        await Promise.race([
          playoutDrained,
          new Promise((resolve) => setTimeout(resolve, 15_000)),
        ]);
      }
    } catch (error) {
      this.setError(error.message);
      byId('voice-test-result').textContent = 'Voice test failed.';
    } finally {
      await client.stop();
      this.offerRecordingDownload(client);
      this.refreshControls();
    }
  }

  /** Expose the captured converted audio as a download link.
   *
   * Diagnostic: separates "the browser never received converted audio" from
   * "it received it but you did not hear it" (device routing, app volume, a
   * suspended AudioContext). The bytes here are captured off the socket, so a
   * playable file with sound proves delivery worked and isolates the fault to
   * playback.
   */
  offerRecordingDownload(client) {
    const slot = byId('voice-test-download');
    if (!slot) return;
    slot.replaceChildren();

    const blob = client.buildRecordingBlob?.();
    if (!blob) {
      slot.textContent = 'No converted audio bytes reached the browser (nothing to save).';
      return;
    }

    const seconds = client.recordedBytes / (OUTPUT_SAMPLE_RATE * 2);
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `keira-test-${new Date().toISOString().replace(/[:.]/g, '-')}.wav`;
    link.textContent = `Download converted audio (${seconds.toFixed(1)}s, ${(blob.size / 1024).toFixed(0)} KB)`;
    slot.appendChild(link);

    const player = document.createElement('audio');
    player.controls = true;
    player.src = link.href;
    slot.appendChild(player);

    // Locate any loss. The relay's own accounting (`playout_sent_bytes`) only
    // arrives with the server's `stopped` message, which this flow usually
    // does NOT see -- runVoiceTest closes the socket from the client side
    // first. So report it when present and stay quiet about it otherwise,
    // rather than implying a comparison that was never made.
    const toSec = (b) => (b / (OUTPUT_SAMPLE_RATE * 2)).toFixed(1);
    const parts = [`browser captured ${toSec(client.recordedBytes)}s off the socket`];
    if (Number.isFinite(client.serverSentBytes)) {
      parts.unshift(`relay sent ${toSec(client.serverSentBytes)}s`);
    }
    if (client.serverDropBytes > 0) {
      parts.push(`relay dropped ${toSec(client.serverDropBytes)}s to buffer overflow`);
    }
    const note = document.createElement('p');
    note.textContent = parts.join(' · ');
    slot.appendChild(note);
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => new DesktopSetupPage().init());
}
