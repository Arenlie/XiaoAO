class Pcm16CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.pending = [];
    this.blockSamples = Math.max(256, Math.round(sampleRate * 0.08));
    this.port.onmessage = (event) => {
      if (event.data?.type === "flush") this.flush();
    };
  }

  process(inputs) {
    const channel = inputs?.[0]?.[0];
    if (!channel?.length) return true;
    for (let i = 0; i < channel.length; i += 1) this.pending.push(channel[i]);
    while (this.pending.length >= this.blockSamples) this.emit(this.blockSamples);
    return true;
  }

  flush() {
    if (this.pending.length >= Math.max(64, Math.round(sampleRate * 0.01))) {
      this.emit(this.pending.length);
    }
  }

  emit(count) {
    const input = Float32Array.from(this.pending.splice(0, count));
    const ratio = sampleRate / 16000;
    const outputLength = Math.max(1, Math.round(input.length / ratio));
    const pcm = new Int16Array(outputLength);
    for (let i = 0; i < outputLength; i += 1) {
      const position = i * ratio;
      const left = Math.min(input.length - 1, Math.floor(position));
      const right = Math.min(input.length - 1, left + 1);
      const frac = position - left;
      const sample = input[left] + (input[right] - input[left]) * frac;
      const clipped = Math.max(-1, Math.min(1, sample));
      pcm[i] = clipped < 0 ? Math.round(clipped * 32768) : Math.round(clipped * 32767);
    }
    this.port.postMessage({ type: "pcm", buffer: pcm.buffer }, [pcm.buffer]);
  }
}

registerProcessor("pcm16-capture", Pcm16CaptureProcessor);
