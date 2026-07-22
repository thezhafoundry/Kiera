const CAPTURE_SAMPLE_RATE = 48_000;
const INPUT_SAMPLE_RATE = 16_000;
const OUTPUT_SAMPLE_RATE = 48_000;

const defaultWebSocketFactory = (url, protocols) => new WebSocket(url, protocols);
const defaultAudioContextFactory = (options) => new AudioContext(options);

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
    this.emitStatus({ type: 'starting' });
    try {
      this.context = this.createAudioContext({ sampleRate: CAPTURE_SAMPLE_RATE });
      if (this.context.sampleRate !== CAPTURE_SAMPLE_RATE) {
        throw new Error(`AudioContext must run at ${CAPTURE_SAMPLE_RATE} Hz; got ${this.context.sampleRate} Hz`);
      }
      if (outputDeviceId && typeof this.context.setSinkId === 'function') {
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
        if (data.pcm instanceof ArrayBuffer && data.pcm.byteLength === 640 && this.socket?.readyState === 1) {
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
