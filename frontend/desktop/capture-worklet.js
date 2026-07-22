import {
  createDownsampleState,
  downsample48kTo16k,
  float32ToPcm16,
  mixToMono,
  takeFrames,
} from './audio_protocol.js';

const CAPTURE_SAMPLE_RATE = 48_000;
const FRAME_SAMPLES = 320;

const appendSamples = (left, right) => {
  const combined = new Float32Array(left.length + right.length);
  combined.set(left);
  combined.set(right, left.length);
  return combined;
};

class KeiraCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.pending = new Float32Array(0);
    this.downsampleState = createDownsampleState();
    this.reportedRateError = false;
    this.blockCount = 0;
  }

  process(inputs) {
    if (sampleRate !== CAPTURE_SAMPLE_RATE) {
      if (!this.reportedRateError) {
        this.reportedRateError = true;
        this.port.postMessage({
          type: 'error',
          message: `Capture AudioContext must run at ${CAPTURE_SAMPLE_RATE} Hz; got ${sampleRate} Hz`,
        });
      }
      return true;
    }

    const channels = inputs[0];
    if (!channels || channels.length === 0) {
      return true;
    }

    const mono = mixToMono(channels);
    const downsampled = downsample48kTo16k(mono, this.downsampleState);
    const framed = takeFrames(appendSamples(this.pending, downsampled), FRAME_SAMPLES);
    this.pending = framed.remainder;

    for (const frame of framed.frames) {
      const pcm = float32ToPcm16(frame);
      this.port.postMessage({ type: 'frame', pcm: pcm.buffer }, [pcm.buffer]);
    }

    this.blockCount += 1;
    if (this.blockCount % 8 === 0) {
      let energy = 0;
      for (const value of mono) {
        energy += value * value;
      }
      this.port.postMessage({
        type: 'meter',
        input: mono.length ? Math.sqrt(energy / mono.length) : 0,
      });
    }
    return true;
  }
}

registerProcessor('keira-capture', KeiraCaptureProcessor);
