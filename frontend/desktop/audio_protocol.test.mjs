import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createDownsampleState,
  downsample48kTo16k,
  float32ToPcm16,
  mixToMono,
  pcm16ToFloat32,
  takeFrames,
} from './audio_protocol.js';

const rms = (samples) => Math.sqrt(
  samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length,
);

const toneAt48k = (frequency, sampleCount = 48_000) => Float32Array.from(
  { length: sampleCount },
  (_, index) => Math.sin((2 * Math.PI * frequency * index) / 48_000),
);

test('mixToMono averages every channel sample', () => {
  const mixed = mixToMono([
    new Float32Array([1, -0.5, 0.25]),
    new Float32Array([-1, 0.5, 0.75]),
  ]);

  assert.deepEqual([...mixed], [0, 0, 0.5]);
});

test('float32ToPcm16 encodes 0.5 as signed little-endian PCM', () => {
  assert.deepEqual([...float32ToPcm16(new Float32Array([0.5]))], [0, 64]);
});

test('PCM conversion clips endpoints and decodes little-endian samples', () => {
  const encoded = float32ToPcm16(new Float32Array([1, -1, 2, -2]));

  assert.deepEqual([...encoded], [255, 127, 0, 128, 255, 127, 0, 128]);
  assert.deepEqual([...pcm16ToFloat32(encoded.buffer)], [
    32767 / 32768,
    -1,
    32767 / 32768,
    -1,
  ]);
});

test('pcm16ToFloat32 rejects a truncated PCM byte sequence', () => {
  assert.throws(
    () => pcm16ToFloat32(new Uint8Array([0, 0, 0]).buffer),
    /even number of bytes/,
  );
});

test('downsample48kTo16k emits 320 samples from one 960-sample capture block', () => {
  const input = Float32Array.from({ length: 960 }, (_, index) => index / 960);
  const output = downsample48kTo16k(input, createDownsampleState());

  assert.equal(output.length, 320);
});

test('downsample48kTo16k retains partial input in caller-owned state', () => {
  const state = createDownsampleState();

  assert.equal(downsample48kTo16k(new Float32Array([1, 2, 3, 4]), state).length, 1);
  assert.deepEqual([...state.carry], [4]);
  assert.equal(downsample48kTo16k(new Float32Array([5, 6]), state).length, 1);
  assert.deepEqual([...state.carry], []);
});

test('downsample48kTo16k is continuous across incomplete input groups', () => {
  const input = Float32Array.from({ length: 97 }, (_, index) => Math.sin(index / 3));
  const complete = downsample48kTo16k(input, createDownsampleState());
  const state = createDownsampleState();
  const streamed = [
    ...downsample48kTo16k(input.subarray(0, 1), state),
    ...downsample48kTo16k(input.subarray(1, 47), state),
    ...downsample48kTo16k(input.subarray(47), state),
  ];

  assert.deepEqual(streamed, [...complete]);
});

test('downsample48kTo16k attenuates a 12 kHz tone before decimation', () => {
  const state = createDownsampleState();
  const output = downsample48kTo16k(toneAt48k(12_000), state);

  assert.ok(rms(output.subarray(128)) < 0.03, '12 kHz alias should be strongly attenuated');
});

test('downsample48kTo16k preserves the passband and rejects tones above output Nyquist', () => {
  const passband = downsample48kTo16k(toneAt48k(4_000), createDownsampleState());
  const stopband = downsample48kTo16k(toneAt48k(8_200), createDownsampleState());

  assert.ok(rms(passband.subarray(128)) > 0.65, '4 kHz tone should remain in the passband');
  assert.ok(rms(stopband.subarray(128)) < 0.05, '8.2 kHz tone should not alias into 16 kHz audio');
});

test('takeFrames returns only complete frames and preserves the remainder', () => {
  const input = Float32Array.from({ length: 650 }, (_, index) => index);
  const { frames, remainder } = takeFrames(input, 320);

  assert.equal(frames.length, 2);
  assert.equal(frames[0].length, 320);
  assert.equal(frames[1].length, 320);
  assert.deepEqual([...remainder], [640, 641, 642, 643, 644, 645, 646, 647, 648, 649]);
});
