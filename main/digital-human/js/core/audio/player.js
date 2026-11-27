// 音频播放模块
import { log } from '../../utils/logger.js?v=0205';
import { createStreamingContext } from './stream-context.js?v=20261020-delivery';

// 音频播放器类
export class AudioPlayer {
    constructor() {
        // 音频参数
        this.SAMPLE_RATE = 16000;
        this.CHANNELS = 1;
        this.FRAME_SIZE = 960;
        this.MIN_AUDIO_DURATION = 0.12;

        // 状态
        this.audioContext = null;
        this.opusDecoder = null;
        this.streamingContext = null;
        this.generation = 0;
        this.pendingPackets = 0;
        this.audioWork = Promise.resolve();
        this.startPromise = null;
        this.isPlaying = false;
    }

    // 获取或创建AudioContext
    getAudioContext() {
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: this.SAMPLE_RATE,
                latencyHint: 'interactive'
            });
            log('创建音频上下文，采样率: ' + this.SAMPLE_RATE + 'Hz', 'debug');
        }
        return this.audioContext;
    }

    // 初始化Opus解码器
    async initOpusDecoder() {
        if (this.opusDecoder) return this.opusDecoder;

        try {
            if (typeof window.ModuleInstance === 'undefined') {
                if (typeof Module !== 'undefined') {
                    window.ModuleInstance = Module;
                    log('使用全局Module作为ModuleInstance', 'info');
                } else {
                    throw new Error('Opus库未加载，ModuleInstance和Module对象都不存在');
                }
            }

            const mod = window.ModuleInstance;

            this.opusDecoder = {
                channels: this.CHANNELS,
                rate: this.SAMPLE_RATE,
                frameSize: this.FRAME_SIZE,
                module: mod,
                decoderPtr: null,

                init: function () {
                    if (this.decoderPtr) return true;

                    const decoderSize = mod._opus_decoder_get_size(this.channels);
                    log(`Opus解码器大小: ${decoderSize}字节`, 'debug');

                    this.decoderPtr = mod._malloc(decoderSize);
                    if (!this.decoderPtr) {
                        throw new Error("无法分配解码器内存");
                    }

                    const err = mod._opus_decoder_init(
                        this.decoderPtr,
                        this.rate,
                        this.channels
                    );

                    if (err < 0) {
                        this.destroy();
                        throw new Error(`Opus解码器初始化失败: ${err}`);
                    }

                    log("Opus解码器初始化成功", 'success');
                    return true;
                },

                decode: function (opusData) {
                    if (!this.decoderPtr) {
                        if (!this.init()) {
                            throw new Error("解码器未初始化且无法初始化");
                        }
                    }

                    try {
                        const mod = this.module;

                        const opusPtr = mod._malloc(opusData.length);
                        mod.HEAPU8.set(opusData, opusPtr);

                        const pcmPtr = mod._malloc(this.frameSize * 2);

                        const decodedSamples = mod._opus_decode(
                            this.decoderPtr,
                            opusPtr,
                            opusData.length,
                            pcmPtr,
                            this.frameSize,
                            0
                        );

                        if (decodedSamples < 0) {
                            mod._free(opusPtr);
                            mod._free(pcmPtr);
                            throw new Error(`Opus解码失败: ${decodedSamples}`);
                        }

                        const decodedData = new Int16Array(decodedSamples);
                        for (let i = 0; i < decodedSamples; i++) {
                            decodedData[i] = mod.HEAP16[(pcmPtr >> 1) + i];
                        }

                        mod._free(opusPtr);
                        mod._free(pcmPtr);

                        return decodedData;
                    } catch (error) {
                        log(`Opus解码错误: ${error.message}`, 'error');
                        return new Int16Array(0);
                    }
                },

                destroy: function () {
                    if (this.decoderPtr) {
                        this.module._free(this.decoderPtr);
                        this.decoderPtr = null;
                    }
                }
            };

            if (!this.opusDecoder.init()) {
                throw new Error("Opus解码器初始化失败");
            }

            return this.opusDecoder;

        } catch (error) {
            log(`Opus解码器初始化失败: ${error.message}`, 'error');
            this.opusDecoder = null;
            throw error;
        }
    }

    // Serialize initialization and decoding so cancellation cannot resurrect packets.
    enqueueAudioData(opusData) {
        if (!opusData.length) return;
        const generation = this.generation;
        this.pendingPackets++;
        this.audioWork = this.audioWork.then(async () => {
            try {
                if (generation !== this.generation) return;
                await this.start();
                if (generation !== this.generation) return;
                this.streamingContext.pushAudioBuffer([opusData]);
            } finally {
                if (generation === this.generation) this.pendingPackets--;
            }
        }).catch(error => log(`音频处理失败: ${error.message}`, 'error'));
    }

    async finishAudio() {
        const generation = this.generation;
        // Capture all packets preceding the stop control message.
        await this.audioWork;
        if (generation !== this.generation) return false;
        return this.streamingContext ? this.streamingContext.finish() : true;
    }

    // 预加载解码器
    async preload() {
        log('预加载Opus解码器...', 'info');
        try {
            await this.initOpusDecoder();
            log('Opus解码器预加载成功', 'success');
        } catch (error) {
            log(`Opus解码器预加载失败: ${error.message}，将在需要时重试`, 'warning');
        }
    }

    // 启动播放系统
    async start() {
        if (!this.startPromise) {
            this.startPromise = (async () => {
                const context = this.getAudioContext();
                await this.initOpusDecoder();
                this.streamingContext = createStreamingContext(
                    this.opusDecoder, context, this.SAMPLE_RATE,
                    this.CHANNELS, this.MIN_AUDIO_DURATION);
            })().catch(error => {
                this.startPromise = null;
                throw error;
            });
        }
        await this.startPromise;
        // Autoplay policies may leave resume pending until a user gesture.
        // Initializing the page and decoding incoming packets must still finish.
        if (this.audioContext.state === 'suspended') {
            this.audioContext.resume().catch(error => log(`等待音频播放授权: ${error.message}`, 'warning'));
        }
    }

    // 获取音频包统计信息
    getAudioStats() {
        if (!this.streamingContext) {
            return {
                pendingDecode: this.pendingPackets,
                pendingPlay: 0,
                totalPending: this.pendingPackets
            };
        }

        const pendingDecode = this.pendingPackets + this.streamingContext.getPendingDecodeCount();
        const pendingPlay = this.streamingContext.getPendingPlayCount();

        return {
            pendingDecode,  // 待解码包数
            pendingPlay,    // 待播放包数
            totalPending: pendingDecode + pendingPlay  // 总待处理包数
        };
    }

    // 清空所有音频缓冲并停止播放
    clearAllAudio() {
        log('AudioPlayer: 清空所有音频', 'info');

        // 清空接收队列（使用clear方法保持对象引用）
        this.generation++;
        this.pendingPackets = 0;

        // 清空流上下文的所有缓冲
        if (this.streamingContext) {
            this.streamingContext.clearAllBuffers();
        }

        log('AudioPlayer: 音频已清空', 'success');
    }
}

// 创建单例
let audioPlayerInstance = null;

export function getAudioPlayer() {
    if (!audioPlayerInstance) {
        audioPlayerInstance = new AudioPlayer();
    }
    return audioPlayerInstance;
}
