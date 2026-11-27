import { log } from '../../utils/logger.js?v=0205';

// Event-driven scheduling. EOS flushes short frames; completion waits for all sources.
export class StreamingContext {
    constructor(opusDecoder, audioContext, sampleRate, channels, minAudioDuration) {
        this.opusDecoder = opusDecoder;
        this.audioContext = audioContext;
        this.sampleRate = sampleRate;
        this.channels = channels;
        this.minAudioDuration = minAudioDuration;
        this.queue = [];
        this.sources = new Set();
        this.playing = false;
        this.endOfStream = false;
        this.totalSamples = 0;
        this.scheduledEndTime = 0;
        this.finishWaiters = [];
        this.analyser = audioContext.createAnalyser();
        this.analyser.fftSize = 256;
    }

    pushAudioBuffer(packets) {
        this.endOfStream = false;
        for (const packet of packets) {
            try {
                const pcm = this.opusDecoder.decode(packet);
                for (const sample of pcm) this.queue.push(sample / 32768.0);
                this.totalSamples += pcm.length;
            } catch (error) {
                log(`Opus解码失败: ${error.message}`, 'error');
            }
        }
        this.schedule();
    }

    schedule() {
        const chunk = Math.max(1, Math.floor(this.sampleRate * this.minAudioDuration));
        // EOS bypasses both initial and chunk thresholds: no stranded tail.
        if (!this.playing && !this.endOfStream && this.queue.length < chunk * 2) return;
        while (this.queue.length >= chunk || (this.endOfStream && this.queue.length)) {
            const samples = this.queue.splice(0, chunk);
            const buffer = this.audioContext.createBuffer(this.channels, samples.length, this.sampleRate);
            buffer.copyToChannel(new Float32Array(samples), 0);
            const source = this.audioContext.createBufferSource();
            source.buffer = buffer;
            source.connect(this.analyser);
            source.connect(this.audioContext.destination);
            source.onended = () => {
                this.sources.delete(source);
                source.disconnect();
                this.completeIfDrained();
            };
            this.sources.add(source);
            const start = Math.max(this.audioContext.currentTime, this.scheduledEndTime);
            source.start(start);
            this.scheduledEndTime = start + buffer.duration;
            this.playing = true;
        }
        this.completeIfDrained();
    }

    finish() {
        this.endOfStream = true;
        return new Promise(resolve => {
            this.finishWaiters.push(resolve);
            this.schedule();
        });
    }

    completeIfDrained() {
        if (this.endOfStream && !this.queue.length && !this.sources.size) {
            this.playing = false;
            for (const resolve of this.finishWaiters.splice(0)) resolve(true);
        }
    }

    clearAllBuffers() {
        this.queue = [];
        for (const source of this.sources) {
            source.onended = null;
            try { source.stop(); } catch (_) { /* Already ended. */ }
            source.disconnect();
        }
        this.sources.clear();
        this.playing = false;
        this.endOfStream = false;
        this.scheduledEndTime = this.audioContext.currentTime;
        this.totalSamples = 0;
        for (const resolve of this.finishWaiters.splice(0)) resolve(false);
    }

    getPendingDecodeCount() { return 0; }
    getPendingPlayCount() {
        const scheduled = this.sources.size
            ? Math.max(0, this.scheduledEndTime - this.audioContext.currentTime) * this.sampleRate : 0;
        return Math.ceil((this.queue.length + scheduled) / 960);
    }
    getAnalyser() { return this.analyser; }
}

export function createStreamingContext(opusDecoder, audioContext, sampleRate, channels, minAudioDuration) {
    return new StreamingContext(opusDecoder, audioContext, sampleRate, channels, minAudioDuration);
}
