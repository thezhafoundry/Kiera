/**
 * Numerical helpers shared by the desktop capture page and its audio worklets.
 * All audio values are normalized Float32 PCM unless otherwise documented.
 */

const DOWNSAMPLE_FACTOR = 3;
const INPUT_SAMPLE_RATE = 48_000;
const CUTOFF_HZ = 7_200;
const FILTER_TAPS = 63;
const HISTORY_SAMPLES = FILTER_TAPS - 1;

const sinc = (value) => (value === 0 ? 1 : Math.sin(Math.PI * value) / (Math.PI * value));

const makeLowPassCoefficients = () => {
  const cutoff = CUTOFF_HZ / INPUT_SAMPLE_RATE;
  const center = (FILTER_TAPS - 1) / 2;
  const coefficients = new Float32Array(FILTER_TAPS);
  let sum = 0;

  for (let tap = 0; tap < FILTER_TAPS; tap += 1) {
    const offset = tap - center;
    const hann = 0.5 - 0.5 * Math.cos((2 * Math.PI * tap) / (FILTER_TAPS - 1));
    const coefficient = 2 * cutoff * sinc(2 * cutoff * offset) * hann;
    coefficients[tap] = coefficient;
    sum += coefficient;
  }

  for (let tap = 0; tap < FILTER_TAPS; tap += 1) {
    coefficients[tap] /= sum;
  }

  return coefficients;
};

const LOW_PASS_COEFFICIENTS = makeLowPassCoefficients();
const POLYPHASE_COEFFICIENTS = Array.from(
  { length: DOWNSAMPLE_FACTOR },
  (_, phase) => {
    const coefficients = [];
    for (let tap = phase; tap < FILTER_TAPS; tap += DOWNSAMPLE_FACTOR) {
      coefficients.push(LOW_PASS_COEFFICIENTS[tap]);
    }
    return coefficients;
  },
);

/** Creates isolated resampling state for one capture stream. */
export const createDownsampleState = () => ({
  carry: new Float32Array(0),
  history: new Float32Array(HISTORY_SAMPLES),
});

/** Averages equally-sized channels into one mono channel. */
export const mixToMono = (channels) => {
  if (channels.length === 0) {
    return new Float32Array(0);
  }

  const sampleCount = channels[0].length;
  if (channels.some((channel) => channel.length !== sampleCount)) {
    throw new RangeError('All channels must have the same sample count');
  }

  const mono = new Float32Array(sampleCount);
  for (const channel of channels) {
    for (let index = 0; index < sampleCount; index += 1) {
      mono[index] += channel[index] / channels.length;
    }
  }
  return mono;
};

/**
 * Decimates 48 kHz PCM to 16 kHz with a Hann-windowed 7.2 kHz FIR filter.
 * `state` is caller-owned and must be retained for the lifetime of a stream.
 */
export const downsample48kTo16k = (input, state) => {
  if (!state || !(state.carry instanceof Float32Array) || !(state.history instanceof Float32Array)) {
    throw new TypeError('A state returned by createDownsampleState is required');
  }

  const available = new Float32Array(state.carry.length + input.length);
  available.set(state.carry);
  available.set(input, state.carry.length);

  const completeSamples = available.length - (available.length % DOWNSAMPLE_FACTOR);
  state.carry = available.slice(completeSamples);
  if (completeSamples === 0) {
    return new Float32Array(0);
  }

  const samples = available.subarray(0, completeSamples);
  const source = new Float32Array(state.history.length + samples.length);
  source.set(state.history);
  source.set(samples, state.history.length);
  const output = new Float32Array(completeSamples / DOWNSAMPLE_FACTOR);

  for (let outputIndex = 0; outputIndex < output.length; outputIndex += 1) {
    const currentSample = state.history.length + (outputIndex * DOWNSAMPLE_FACTOR) + 2;
    let filtered = 0;

    for (let phase = 0; phase < DOWNSAMPLE_FACTOR; phase += 1) {
      const coefficients = POLYPHASE_COEFFICIENTS[phase];
      for (let index = 0; index < coefficients.length; index += 1) {
        filtered += coefficients[index] * source[currentSample - phase - (index * DOWNSAMPLE_FACTOR)];
      }
    }

    output[outputIndex] = filtered;
  }

  state.history = source.slice(-HISTORY_SAMPLES);
  return output;
};

/** Encodes normalized Float32 PCM as little-endian signed 16-bit PCM. */
export const float32ToPcm16 = (input) => {
  const output = new Uint8Array(input.length * 2);
  const view = new DataView(output.buffer);

  for (let index = 0; index < input.length; index += 1) {
    const sample = Math.max(-1, Math.min(1, input[index]));
    const integer = sample < 0 ? Math.round(sample * 32768) : Math.round(sample * 32767);
    view.setInt16(index * 2, integer, true);
  }
  return output;
};

/** Decodes little-endian signed 16-bit PCM into normalized Float32 PCM. */
export const pcm16ToFloat32 = (input) => {
  if (input.byteLength % 2 !== 0) {
    throw new RangeError('PCM16 input must contain an even number of bytes');
  }

  const view = new DataView(input);
  const output = new Float32Array(input.byteLength / 2);

  for (let index = 0; index < output.length; index += 1) {
    output[index] = view.getInt16(index * 2, true) / 32768;
  }
  return output;
};

/** Splits a sample buffer into complete fixed-size frames. */
export const takeFrames = (buffer, frameSamples) => {
  if (!Number.isInteger(frameSamples) || frameSamples <= 0) {
    throw new RangeError('frameSamples must be a positive integer');
  }

  const frameCount = Math.floor(buffer.length / frameSamples);
  const frames = Array.from(
    { length: frameCount },
    (_, index) => buffer.slice(index * frameSamples, (index + 1) * frameSamples),
  );
  return { frames, remainder: buffer.slice(frameCount * frameSamples) };
};
