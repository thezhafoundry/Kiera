import assert from 'node:assert/strict';
import test from 'node:test';

import { DesktopAudioClient, DesktopSetupPage } from './desktop.js';

function wireCaptureClient() {
  const sent = [];
  const client = new DesktopAudioClient();
  client.socket = { readyState: 1, send: (frame) => sent.push(frame) };
  client.captureNode = { port: {} };
  client.playoutNode = { port: {} };
  client.bindWorklets();
  return { client, sent };
}

function makeStartClient() {
  const socket = { readyState: 1, send() {}, close() {} };
  const context = {
    sampleRate: 48_000,
    destination: {},
    audioWorklet: { addModule: async () => {} },
    createMediaStreamSource: () => ({ connect() {}, disconnect() {} }),
    resume: async () => {},
    close: async () => {},
  };
  const mediaDevices = {
    getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }),
  };
  class WorkletNode {
    constructor() {
      this.port = {};
    }
    connect() {}
    disconnect() {}
  }
  const client = new DesktopAudioClient({
    createAudioContext: () => context,
    createWebSocket: () => {
      queueMicrotask(() => socket.onopen());
      return socket;
    },
    mediaDevices,
    location: { protocol: 'http:', host: 'example.test' },
    AudioWorkletNodeClass: WorkletNode,
  });
  return client;
}

function makePage() {
  const page = new DesktopSetupPage();
  page.setState = (state) => { page.state = state; };
  page.setError = () => {};
  page.updateMeters = () => {};
  return page;
}

test('capture frames are blocked until relay ready and forwarded afterward', () => {
  const { client, sent } = wireCaptureClient();
  const frame = new ArrayBuffer(640);

  client.captureNode.port.onmessage({ data: { type: 'frame', pcm: frame } });
  assert.equal(sent.length, 0);

  client.handleSocketMessage(JSON.stringify({ type: 'ready' }));
  client.captureNode.port.onmessage({ data: { type: 'frame', pcm: frame } });
  assert.equal(sent.length, 1);
});

test('relay readiness resets on stop and a new start', async () => {
  const client = makeStartClient();
  client.relayReady = true;
  client.socket = { readyState: 3 };
  await client.stop();
  assert.equal(client.relayReady, false);

  client.relayReady = true;
  await client.start({ ticket: 'test-ticket' });
  assert.equal(client.relayReady, false);
  await client.stop();
});

test('page remains warming when connected precedes relay ready', () => {
  const page = makePage();
  let emitStatus;
  page.bindClient({ onStatus: (callback) => { emitStatus = callback; }, onMeters() {} });

  emitStatus({ type: 'connected' });
  assert.equal(page.state, 'warming');
  emitStatus({ type: 'ready' });
  assert.equal(page.state, 'ready');
  assert.equal(page.backendReady, true);
});

test('page transitions to converting when connected follows relay ready', () => {
  const page = makePage();
  let emitStatus;
  page.bindClient({ onStatus: (callback) => { emitStatus = callback; }, onMeters() {} });

  emitStatus({ type: 'ready' });
  assert.equal(page.state, 'ready');
  emitStatus({ type: 'connected' });
  assert.equal(page.state, 'converting');
});

test('local no-auth mode allows starting without a control token', () => {
  const page = makePage();
  page.authModeReady = true;
  page.authRequired = false;
  page.tokenInput = { value: '' };
  page.selectedInput = () => ({ label: 'MacBook Microphone' });
  page.selectedOutput = () => ({ label: 'BlackHole 2ch' });
  page.state = 'stopped';
  page.client = null;

  const previousAudioContext = globalThis.AudioContext;
  globalThis.AudioContext = class {};
  globalThis.AudioContext.prototype.setSinkId = async () => {};
  try {
    assert.equal(page.canStart(), true);
  } finally {
    globalThis.AudioContext = previousAudioContext;
  }
});

test('tokenless mode permits desktop session actions without a token', () => {
  const page = makePage();
  page.authModeReady = true;
  page.authRequired = false;
  page.tokenInput = { value: '' };

  assert.equal(page.canUseDesktopSession(), true);
});

test('virtual loopback devices are rejected as microphone inputs', () => {
  const page = makePage();
  page.authModeReady = true;
  page.authRequired = false;
  page.tokenInput = { value: '' };
  page.selectedInput = () => ({ label: 'BlackHole 2ch' });
  page.selectedOutput = () => ({ label: 'BlackHole 2ch' });
  page.state = 'stopped';

  const previousAudioContext = globalThis.AudioContext;
  globalThis.AudioContext = class {};
  globalThis.AudioContext.prototype.setSinkId = async () => {};
  try {
    assert.equal(page.canStart(), false);
  } finally {
    globalThis.AudioContext = previousAudioContext;
  }
});

test('default fetchImpl is bound and callable detached from its owner', async () => {
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async function boundCheckingFetch(url) {
    assert.equal(this, globalThis, 'fetch must be invoked with globalThis as receiver');
    return { json: async () => ({ url }) };
  };
  try {
    const page = new DesktopSetupPage();
    const detached = page.fetchImpl;
    await assert.doesNotReject(detached('/api/desktop/auth-mode'));
  } finally {
    globalThis.fetch = previousFetch;
  }
});

test('warmGpu reports success and re-enables the button', async () => {
  const page = makePage();
  page.warmButton = { disabled: false };
  page.warmResult = { textContent: '' };
  page.fetchImpl = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ status: 'success', message: 'GPU warmed up successfully.' }),
  });

  await page.warmGpu();

  assert.equal(page.warmButton.disabled, false);
  assert.equal(page.warmResult.textContent, 'GPU warm and ready.');
});

test('warmGpu surfaces the server message on failure', async () => {
  const page = makePage();
  page.warmButton = { disabled: false };
  page.warmResult = { textContent: '' };
  page.fetchImpl = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ status: 'error', message: 'GPU did not become ready within 360s.' }),
  });

  await page.warmGpu();

  assert.equal(page.warmResult.textContent, 'GPU warmup failed: GPU did not become ready within 360s.');
});

test('voice test rejects virtual loopback microphone inputs', () => {
  const page = makePage();
  page.authModeReady = true;
  page.authRequired = false;
  page.tokenInput = { value: '' };
  page.selectedInput = () => ({ label: 'Loopback Audio' });

  assert.equal(page.canRunVoiceTest(), false);
});

/**
 * Fake client that reproduces the measured live relay timing: the `ready`
 * handshake lands after `readyDelayMs`, and the first converted frame arrives
 * `audioAfterReadyMs` later (measured warm: ~1.9s then ~1.36s).
 */
function makeTimedVoiceTestClient({ readyDelayMs, audioAfterReadyMs }) {
  const statusCallbacks = new Set();
  const meterCallbacks = new Set();
  const timers = [];
  const client = {
    stopped: false,
    stoppedAt: null,
    startedAt: null,
    onStatus(cb) { statusCallbacks.add(cb); },
    onMeters(cb) { meterCallbacks.add(cb); },
    async start() {
      client.startedAt = Date.now();
      timers.push(setTimeout(() => {
        for (const cb of statusCallbacks) cb({ type: 'ready' });
        timers.push(setTimeout(() => {
          for (const cb of meterCallbacks) cb({ input: 0, output: 0.5, bufferMs: 40 });
          timers.push(setTimeout(() => {
            for (const cb of meterCallbacks) cb({ input: 0, output: 0, bufferMs: 0 });
          }, 20));
        }, audioAfterReadyMs));
      }, readyDelayMs));
    },
    async stop() {
      client.stopped = true;
      client.stoppedAt = Date.now();
      for (const t of timers) clearTimeout(t);
    },
  };
  return client;
}

function makeVoiceTestPage(client) {
  const elements = {
    'voice-test-result': { textContent: '' },
    'voice-test-latency': { textContent: '' },
    'input-meter-fill': { style: {} },
    'input-meter-value': { textContent: '' },
    'output-meter-fill': { style: {} },
    'output-meter-value': { textContent: '' },
    'playout-buffer': { textContent: '' },
    'input-drops': { textContent: '' },
    'playout-drops': { textContent: '' },
    'reconnect-count': { textContent: '' },
    'connection-state': { textContent: '', dataset: {} },
    'page-error': { textContent: '', hidden: true },
    'device-warning': { textContent: '', hidden: true },
  };
  globalThis.document = { getElementById: (id) => elements[id] };

  const page = new DesktopSetupPage({ createClient: () => client });
  page.authModeReady = true;
  page.authRequired = false;
  page.tokenInput = { value: '' };
  page.testButton = { disabled: false };
  page.inputSelect = { value: 'mic-1' };
  page.profileInput = { value: 'male' };
  page.selectedInput = () => ({ label: 'Built-in Microphone' });
  page.requestTicket = async () => 'ticket-abc';
  page.refreshControls = () => {};
  page.setState = (state) => { page.state = state; };
  page.setError = () => {};
  return { page, elements };
}

test('voice test waits for converted audio at real pipeline latency', async () => {
  // Measured live against the deployed relay: ready ~1.9s, first converted
  // frame ~1.36s after that. The old implementation used a single 3500ms
  // budget starting at click time, which tore the socket down mid-conversion.
  const client = makeTimedVoiceTestClient({ readyDelayMs: 1900, audioAfterReadyMs: 1400 });
  const { page, elements } = makeVoiceTestPage(client);

  await page.runVoiceTest();

  assert.equal(
    elements['voice-test-result'].textContent.startsWith('Converted audio received'),
    true,
    `expected converted audio to be reported, got: "${elements['voice-test-result'].textContent}"`,
  );
  assert.equal(client.stopped, true, 'client should still be torn down afterward');
});

test('voice test does not fold a slow ready handshake into the audio budget', async () => {
  // A cold Modal container was measured taking 24s+ just to return `ready`.
  // The audio budget must start from `ready`, not from the click.
  const client = makeTimedVoiceTestClient({ readyDelayMs: 6000, audioAfterReadyMs: 1400 });
  const { page, elements } = makeVoiceTestPage(client);

  await page.runVoiceTest();

  assert.equal(
    elements['voice-test-result'].textContent.startsWith('Converted audio received'),
    true,
    `slow handshake should not cause a false "no audio" failure, got: "${elements['voice-test-result'].textContent}"`,
  );
});
