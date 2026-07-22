const CAPTURE_SAMPLE_RATE = 48_000;
const INPUT_SAMPLE_RATE = 16_000;
const OUTPUT_SAMPLE_RATE = 48_000;

const defaultWebSocketFactory = (url, protocols) => new WebSocket(url, protocols);
const defaultAudioContextFactory = (options) => new AudioContext(options);

export const isCableInputSink = (label = '') => /cable\s+input/i.test(label);
export const isCableOutputInput = (label = '') => /cable\s+output/i.test(label);

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
      this.playoutNode?.port.postMessage({ type: 'audio', pcm: message }, [message]);
      this.emitMeters({ input: 0, output: rmsPcm16(message), bufferMs: 0 });
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
      this.emitStatus(status);
      if (status.type === 'error') {
        this.fail(status.message || 'Desktop audio relay error');
      }
    } catch {
      this.fail('Desktop audio relay sent invalid status data');
    }
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

const byId = (id) => document.getElementById(id);
const formatMs = (value) => Number.isFinite(value) ? `${Math.round(value)} ms` : '--';
const formatPercent = (value) => `${Math.round(Math.min(1, Math.max(0, value || 0)) * 100)}%`;

/** Controls the desktop setup page without persisting its control-plane token. */
export class DesktopSetupPage {
  constructor({ createClient = () => new DesktopAudioClient(), fetchImpl = globalThis.fetch } = {}) {
    this.createClient = createClient;
    this.fetchImpl = fetchImpl;
    this.client = null;
    this.state = 'signed_out';
    this.backendReady = false;
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
    this.tokenInput.addEventListener('input', () => this.onTokenInput());
    this.profileInput.addEventListener('change', () => this.refreshControls());
    this.inputSelect.addEventListener('change', () => this.validateDevices());
    this.outputSelect.addEventListener('change', () => this.validateDevices());
    this.startButton.addEventListener('click', () => void this.startConversion());
    this.stopButton.addEventListener('click', () => void this.stopConversion());
    this.testButton.addEventListener('click', () => void this.runVoiceTest());
    this.setState('signed_out');
    void this.enumerateDevices();
  }

  token() {
    return this.tokenInput.value.trim();
  }

  onTokenInput() {
    if (!this.client) this.setState(this.token() ? 'stopped' : 'signed_out');
    this.refreshControls();
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
      this.populateDevices(this.outputSelect, this.devices.outputs, 'Select CABLE Input');
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
    if (input && isCableOutputInput(input.label)) {
      this.setNotice('CABLE Output is selected as the input. Choose your physical microphone to prevent feedback and raw routing.');
    } else if (output && !isCableInputSink(output.label)) {
      this.setNotice('Converted output must be the VB-CABLE “CABLE Input” device.');
    } else if (output && typeof AudioContext.prototype.setSinkId !== 'function') {
      this.setNotice('AudioContext.setSinkId is required. Use a current Chrome or Edge build on Windows.');
    } else {
      this.setNotice('');
    }
    this.refreshControls();
  }

  canStart() {
    const input = this.selectedInput();
    const output = this.selectedOutput();
    return Boolean(
      this.token()
      && input && !isCableOutputInput(input.label)
      && output && isCableInputSink(output.label)
      && typeof AudioContext.prototype.setSinkId === 'function'
      && !this.client
      && !['starting', 'warming', 'converting'].includes(this.state)
    );
  }

  refreshControls() {
    if (!this.startButton) return;
    this.startButton.disabled = !this.canStart();
    this.stopButton.disabled = !this.client;
    this.testButton.disabled = !this.token() || !this.selectedInput() || ['starting', 'warming', 'converting'].includes(this.state);
  }

  async requestTicket() {
    const response = await this.fetchImpl('/api/desktop/session', {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.token()}`,
        'Content-Type': 'application/json',
      },
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
    this.setState(this.token() ? 'stopped' : 'signed_out');
  }

  async runVoiceTest() {
    if (!this.token() || !this.selectedInput()) return;
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
    this.bindClient(client, { test: true, startedAt });
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
    try {
      const ticket = await this.requestTicket();
      await client.start({ inputDeviceId: this.inputSelect.value, ticket });
      await Promise.race([
        firstAudio,
        new Promise((resolve) => setTimeout(resolve, 3500)),
      ]);
      if (!receivedAudio) byId('voice-test-result').textContent = 'No converted audio returned. Check the relay and voice level.';
      if (receivedAudio) {
        await new Promise((resolve) => setTimeout(resolve, 150));
        await Promise.race([
          playoutDrained,
          new Promise((resolve) => setTimeout(resolve, 1200)),
        ]);
      }
    } catch (error) {
      this.setError(error.message);
      byId('voice-test-result').textContent = 'Voice test failed.';
    } finally {
      await client.stop();
      this.refreshControls();
    }
  }
}

if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => new DesktopSetupPage().init());
}
