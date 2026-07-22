const OUTPUT_SAMPLE_RATE = 48_000;
const MAX_QUEUE_SAMPLES = OUTPUT_SAMPLE_RATE * 5;
const REPORT_INTERVAL_SAMPLES = OUTPUT_SAMPLE_RATE / 10;

const decodePcm16 = (pcm) => {
  if (!(pcm instanceof ArrayBuffer) || pcm.byteLength % 2 !== 0) {
    return new Float32Array(0);
  }

  const view = new DataView(pcm);
  const samples = new Float32Array(pcm.byteLength / 2);
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = view.getInt16(index * 2, true) / 32768;
  }
  return samples;
};

class KeiraPlayoutProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunks = [];
    this.queuedSamples = 0;
    this.oldestDropCount = 0;
    this.underrunCount = 0;
    this.reportedSamples = 0;
    this.port.onmessage = ({ data }) => {
      if (data?.type !== 'audio') {
        return;
      }
      this.enqueue(decodePcm16(data.pcm));
    };
  }

  enqueue(samples) {
    if (samples.length === 0) {
      return;
    }
    this.chunks.push({ samples, offset: 0 });
    this.queuedSamples += samples.length;

    while (this.queuedSamples > MAX_QUEUE_SAMPLES && this.chunks.length > 0) {
      const oldest = this.chunks[0];
      const excess = this.queuedSamples - MAX_QUEUE_SAMPLES;
      const available = oldest.samples.length - oldest.offset;
      const dropped = Math.min(excess, available);
      oldest.offset += dropped;
      this.queuedSamples -= dropped;
      this.oldestDropCount += dropped;
      if (oldest.offset === oldest.samples.length) {
        this.chunks.shift();
      }
    }
  }

  process(_, outputs) {
    const output = outputs[0]?.[0];
    if (!output) {
      return true;
    }

    let energy = 0;
    for (let index = 0; index < output.length; index += 1) {
      let value = 0;
      const chunk = this.chunks[0];
      if (chunk) {
        value = chunk.samples[chunk.offset];
        chunk.offset += 1;
        this.queuedSamples -= 1;
        if (chunk.offset === chunk.samples.length) {
          this.chunks.shift();
        }
      } else {
        this.underrunCount += 1;
      }
      output[index] = value;
      energy += value * value;
    }

    this.reportedSamples += output.length;
    if (this.reportedSamples >= REPORT_INTERVAL_SAMPLES) {
      this.reportedSamples = 0;
      this.port.postMessage({
        type: 'meter',
        output: Math.sqrt(energy / output.length),
        bufferMs: (this.queuedSamples / OUTPUT_SAMPLE_RATE) * 1000,
        oldestDropCount: this.oldestDropCount,
        underrunCount: this.underrunCount,
      });
    }
    return true;
  }
}

registerProcessor('keira-playout', KeiraPlayoutProcessor);
