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
