/* common.js · 三个练习页共用的工具
   包含：请求封装、音频播放、语音合成、录音（MediaRecorder + WAV 回退）、轻提示 */

const Common = (() => {
  let currentAudio = null;

  async function api(url, data) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data || {})
    });
    let json = {};
    try { json = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
    if (!res.ok) {
      const err = new Error(json.error || `请求失败（${res.status}）`);
      err.status = res.status;
      throw err;
    }
    return json;
  }

  async function getJson(url) {
    const res = await fetch(url);
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(json.error || `请求失败（${res.status}）`);
    return json;
  }

  function playUrl(url, onended) {
    return new Promise((resolve, reject) => {
      if (!url) { resolve(false); return; }
      stopAudio();
      const audio = new Audio(url);
      currentAudio = audio;
      audio.onended = () => {
        if (onended) onended();
        resolve(true);
      };
      audio.onerror = () => reject(new Error("音频播放失败：" + url));
      audio.play().catch(reject);
    });
  }

  function stopAudio() {
    if (currentAudio) {
      currentAudio.pause();
      currentAudio.currentTime = 0;
      currentAudio = null;
    }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
  }

  function speak(text, lang = "en-US", rate = 0.9) {
    if (!("speechSynthesis" in window)) return;
    const u = new SpeechSynthesisUtterance(text);
    u.lang = lang;
    u.rate = rate;
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  }

  function toast(message, variant = "secondary") {
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      host.className = "position-fixed top-0 start-50 translate-middle-x p-3";
      host.style.zIndex = "1090";
      document.body.appendChild(host);
    }
    const box = document.createElement("div");
    box.className = `alert alert-${variant} shadow py-2 px-3`;
    box.textContent = message;
    host.appendChild(box);
    setTimeout(() => box.remove(), 2600);
  }

  function wait(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
  }

  function fmtTime(seconds) {
    return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
  }

  /** 倒计时。返回 {promise, stop}：promise 在限时结束时 resolve(false)，
      调用 stop(true) 表示用户已作答并提前结束。onTick 每 100ms 收到剩余秒数。 */
  function startCountdown(seconds, onTick) {
    const deadline = Date.now() + seconds * 1000;
    let settled = false;
    let resolveFn;
    const promise = new Promise(resolve => { resolveFn = resolve; });
    const stop = answered => {
      if (settled) return;
      settled = true;
      clearInterval(timer);
      resolveFn(Boolean(answered));
    };
    const timer = setInterval(() => {
      const left = (deadline - Date.now()) / 1000;
      if (onTick) onTick(Math.max(0, left));
      if (left <= 0) stop(false);
    }, 100);
    return { promise, stop };
  }

  return { api, getJson, playUrl, stopAudio, speak, toast, wait, fmtTime, startCountdown };
})();

/** 录音：优先 MediaRecorder，全部编码都不支持时（部分 iOS Safari）用 Web Audio 自己编码 WAV */
class Recorder {
  constructor() {
    this.stream = null;
    this.mediaRecorder = null;
    this.chunks = [];
    this.mode = null;          // 'mediarecorder' | 'wav'
    this.audioContext = null;
    this.processor = null;
    this.samples = [];
    this.sampleRate = 44100;
  }

  static mimeTypeCandidates() {
    return [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/ogg;codecs=opus",
      "audio/mp4",
      "audio/mpeg"
    ];
  }

  static pickMime() {
    if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return "";
    return Recorder.mimeTypeCandidates().find(m => MediaRecorder.isTypeSupported(m)) || "";
  }

  static supported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  /** 返回本浏览器会走哪条录音路径，供帮助文案与调试使用 */
  static mode() {
    if (!Recorder.supported()) return "none";
    return Recorder.pickMime() ? "mediarecorder" : "wav";
  }

  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime = Recorder.pickMime();
    if (mime) {
      this.mode = "mediarecorder";
      this.mediaRecorder = new MediaRecorder(this.stream, { mimeType: mime });
      this.chunks = [];
      this.mediaRecorder.ondataavailable = e => {
        if (e.data && e.data.size > 0) this.chunks.push(e.data);
      };
      this.mediaRecorder.start();
    } else {
      this.mode = "wav";
      this._startWavCapture();
    }
    return this.mode;
  }

  _startWavCapture() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    this.audioContext = new Ctx();
    this.sampleRate = this.audioContext.sampleRate;
    const source = this.audioContext.createMediaStreamSource(this.stream);
    this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);
    this.samples = [];
    this.processor.onaudioprocess = e => {
      this.samples.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    };
    source.connect(this.processor);
    this.processor.connect(this.audioContext.destination);
  }

  stop() {
    return new Promise(resolve => {
      const release = () => {
        if (this.stream) this.stream.getTracks().forEach(t => t.stop());
        if (this.audioContext) this.audioContext.close();
      };
      if (this.mode === "mediarecorder") {
        this.mediaRecorder.onstop = () => {
          const type = this.mediaRecorder.mimeType || "audio/webm";
          const blob = new Blob(this.chunks, { type });
          release();
          resolve({ blob, ext: Recorder.extOf(type) });
        };
        this.mediaRecorder.stop();
      } else if (this.mode === "wav") {
        this.processor.disconnect();
        const blob = Recorder.encodeWav(this.samples, this.sampleRate);
        release();
        resolve({ blob, ext: "wav" });
      } else {
        resolve({ blob: null, ext: null });
      }
    });
  }

  static extOf(mime) {
    if (mime.includes("webm")) return "webm";
    if (mime.includes("ogg")) return "ogg";
    if (mime.includes("mp4") || mime.includes("m4a")) return "m4a";
    if (mime.includes("mpeg") || mime.includes("mp3")) return "mp3";
    return "webm";
  }

  /** Float32 采样 → 16 位 PCM WAV，作为 MediaRecorder 不可用时的兜底 */
  static encodeWav(channelData, sampleRate) {
    const length = channelData.reduce((sum, arr) => sum + arr.length, 0);
    const buffer = new ArrayBuffer(44 + length * 2);
    const view = new DataView(buffer);
    const writeString = (offset, str) => {
      for (let i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
    };

    writeString(0, "RIFF");
    view.setUint32(4, 36 + length * 2, true);
    writeString(8, "WAVE");
    writeString(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);          // PCM
    view.setUint16(22, 1, true);          // 单声道
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(36, "data");
    view.setUint32(40, length * 2, true);

    let offset = 44;
    for (const chunk of channelData) {
      for (let i = 0; i < chunk.length; i++) {
        const s = Math.max(-1, Math.min(1, chunk[i]));
        view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
        offset += 2;
      }
    }
    return new Blob([view], { type: "audio/wav" });
  }
}

/** 把录音 Blob 上传到后端，返回 {path, url} */
async function uploadRecording(blob, ext, meta = {}) {
  const form = new FormData();
  form.append("audio", blob, `recording.${ext || "webm"}`);
  Object.entries(meta).forEach(([k, v]) => form.append(k, v));
  const res = await fetch("/api/recording", { method: "POST", body: form });
  const json = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(json.error || "录音上传失败");
  return json;
}
