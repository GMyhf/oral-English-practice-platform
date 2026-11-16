/* common.js · 三个练习页共用的工具
   包含：请求封装、音频播放（整页共用一个 <audio>，移动端要先解锁）、语音合成、
        录音（MediaRecorder + WAV 回退，麦克风整流复用）、轻提示 */

const Common = (() => {
  // 整页共用一个 <audio>：iOS Safari 只放行「在用户手势里播过一次」的那个元素，
  // 每题 new Audio() 的话第二题开始就被当成自动播放直接拒了，手机上等于没声音。
  let sharedAudio = null;

  function ensureAudio() {
    if (!sharedAudio) {
      sharedAudio = new Audio();
      sharedAudio.preload = "auto";
    }
    return sharedAudio;
  }

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
      const audio = ensureAudio();
      // 上一题的回调必须先摘掉：换 src 时旧元素的 ended/error 会串到新音频上
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
      if ("speechSynthesis" in window) speechSynthesis.cancel();   // 别和合成音叠在一起
      audio.muted = false;
      audio.src = url;
      audio.onended = () => {
        if (onended) onended();
        resolve(true);
      };
      audio.onerror = () => reject(new Error("音频播放失败：" + url));
      let started;
      try {
        started = audio.play();
      } catch (err) {
        reject(err);
        return;
      }
      if (started && started.catch) started.catch(err => reject(err));
    });
  }

  /** 移动端解锁：Safari 要求音频第一次播放发生在用户手势里，之后同一个元素才允许被脚本唤起。
      练习中每个词都是自动播放，所以在点「开始练习」的那一刻先无声地放一段静音。
      注意不要 play() 完马上 pause()：那样 WebKit 认为「一次都没播起来」，照样不给解锁，
      所以用一段 1 毫秒的静音让它自己放完，muted 保证听不见。 */
  const SILENT_WAV = "data:audio/wav;base64," +
    "UklGRjwAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQgAAAAAAAAAAAAAAA==";

  function unlockAudio() {
    const audio = ensureAudio();
    audio.onended = null;
    audio.onerror = null;
    audio.muted = true;
    audio.src = SILENT_WAV;
    let started;
    try {
      started = audio.play();
    } catch (err) {
      return false;
    }
    if (started && started.catch) started.catch(() => {});   // 桌面浏览器一般直接成功
    return true;
  }

  function stopAudio() {
    if (sharedAudio) {
      sharedAudio.pause();
      sharedAudio.currentTime = 0;
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

  /** 语音列表是异步加载的：浏览器首次 getVoices() 往往返回空数组，
      此时直接 speak() 完全无声。练习开始前先调本方法预热一次。 */
  function warmUpSpeech(timeoutMs = 800) {
    if (!("speechSynthesis" in window)) return Promise.resolve(false);
    if (speechSynthesis.getVoices().length) return Promise.resolve(true);
    return new Promise(resolve => {
      let settled = false;
      const done = () => {
        if (settled) return;
        settled = true;
        speechSynthesis.removeEventListener("voiceschanged", done);
        resolve(speechSynthesis.getVoices().length > 0);
      };
      speechSynthesis.addEventListener("voiceschanged", done);
      setTimeout(done, timeoutMs);
    });
  }

  /** 这台设备到底能不能合成英文发音（没有语音包时听读写等于出静音题） */
  function speechReady() {
    if (!("speechSynthesis" in window)) return false;
    return speechSynthesis.getVoices().some(v => /^en/i.test(v.lang || ""));
  }

  function toast(message, variant = "secondary", ms = 2600) {
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
    setTimeout(() => box.remove(), ms);
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

  return {
    api, getJson, playUrl, stopAudio, unlockAudio, speak, warmUpSpeech, speechReady,
    toast, wait, fmtTime, startCountdown
  };
})();

/** 录音：优先 MediaRecorder，全部编码都不支持时（部分 iOS Safari）用 Web Audio 自己编码 WAV
 *
 *  麦克风是整场练习共用的：每次 getUserMedia() 要 0.1~0.5 秒，逐词重新申请会有两个后果——
 *  「边播边录」时标准音的第一个音节还没进缓冲区就断了，手机浏览器则会反复弹权限。
 *  所以先 Recorder.prime() 拿到一条流（放在用户点击的回调里，权限弹窗只出现一次），
 *  每个词再 Recorder.openStream() 复用同一条流，练习结束 Recorder.releaseStream() 关灯。 */
class Recorder {
  constructor() {
    this.stream = null;
    this.mediaRecorder = null;
    this.chunks = [];
    this.mode = null;          // 'mediarecorder' | 'wav'
    this.audioContext = null;
    this.processor = null;
    this.wavSource = null;
    this.samples = [];
    this.sampleRate = 44100;
    this.ownsStream = true;    // 自建流要在 stop() 里释放，共享流不能动，否则下一题没麦克风
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

  /** 这台设备到底会走哪条录音路径，供帮助文案与调试使用 */
  static mode() {
    if (!Recorder.supported()) return "none";
    return Recorder.pickMime() ? "mediarecorder" : "wav";
  }

  /** 取整场练习共用的麦克风流；已经没了（用户中途在地址栏关掉权限）就重新申请。 */
  static async openStream() {
    const alive = Recorder._sharedStream &&
      Recorder._sharedStream.getAudioTracks().some(t => t.readyState === "live");
    if (alive) return Recorder._sharedStream;
    Recorder.releaseStream();
    Recorder._sharedStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    return Recorder._sharedStream;
  }

  /** 播放 + 录音并发时，麦克风和 AudioContext 都得在用户手势里先建好，
      否则 iOS Safari 会因为「不在手势上下文中」直接拒绝。 */
  static async prime() {
    const stream = await Recorder.openStream();
    if (!Recorder.pickMime()) await Recorder.sharedContext();
    return stream;
  }

  /** Web Audio 兜底用的上下文同样复用：iOS 上 AudioContext 有数量上限，逐词新建会失败 */
  static async sharedContext() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Recorder._sharedContext) Recorder._sharedContext = new Ctx();
    if (Recorder._sharedContext.state === "suspended") {
      await Recorder._sharedContext.resume();
    }
    return Recorder._sharedContext;
  }

  /** 释放麦克风（浏览器那个录音红点）和 Web Audio 上下文 */
  static releaseStream() {
    if (Recorder._sharedStream) {
      Recorder._sharedStream.getTracks().forEach(t => t.stop());
      Recorder._sharedStream = null;
    }
    if (Recorder._sharedContext) {
      Recorder._sharedContext.close();
      Recorder._sharedContext = null;
    }
  }

  /** @param {MediaStream} [stream] 传共享流就只负责录制，不拥有它、结束时也不关掉 */
  async start(stream) {
    this.ownsStream = !stream;
    this.stream = stream || await navigator.mediaDevices.getUserMedia({ audio: true });
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
      await this._startWavCapture();
    }
    return this.mode;
  }

  async _startWavCapture() {
    this.audioContext = await Recorder.sharedContext();
    this.sampleRate = this.audioContext.sampleRate;
    this.wavSource = this.audioContext.createMediaStreamSource(this.stream);
    this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);
    this.samples = [];
    this.processor.onaudioprocess = e => {
      this.samples.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    };
    this.wavSource.connect(this.processor);
    this.processor.connect(this.audioContext.destination);
  }

  stop() {
    return new Promise(resolve => {
      const release = () => {
        // 共享流留给下一题用，只有这次自己申请的才关掉；
        // AudioContext 同理是复用的，close() 交给 releaseStream()
        if (this.ownsStream && this.stream) this.stream.getTracks().forEach(t => t.stop());
        if (this.wavSource) { this.wavSource.disconnect(); this.wavSource = null; }
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

// 共享的麦克风流与 AudioContext。写成普通赋值而不是 static 字段：
// static x = 1 这种类字段要 Safari 14.1 之后才认识，而手机端的用户可能还停在更早的版本。
Recorder._sharedStream = null;
Recorder._sharedContext = null;

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
