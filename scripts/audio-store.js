/* audio-store.js · 本地音频导入（听读写 / 看读译）
   词库单词默认没有真人音频，可以在练习页导入自己的录音：
   文件名与单词同名即自动匹配（abandon.mp3 → abandon），也可把任意文件绑定到当前单词。
   音频 Blob 存 IndexedDB，不占 localStorage 配额。 */

const AudioStore = (() => {
  const DB_NAME = "wt_audio_db";
  const STORE = "clips";
  const VERSION = 1;
  let dbPromise = null;

  function open() {
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        if (!window.indexedDB) { reject(new Error("当前浏览器不支持 IndexedDB")); return; }
        const req = indexedDB.open(DB_NAME, VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, { keyPath: "word" });
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
        req.onblocked = () => reject(new Error("IndexedDB 被其他标签页占用，请关闭后重试"));
      });
      dbPromise.catch(() => { dbPromise = null; });
    }
    return dbPromise;
  }

  function run(mode, fn) {
    return open().then(db => new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, mode);
      const req = fn(tx.objectStore(STORE));
      tx.oncomplete = () => resolve(req ? req.result : undefined);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    }));
  }

  return {
    normalize(name) {
      return String(name || "").trim().toLowerCase().replace(/\s+/g, "");
    },
    put(word, blob, fileName) {
      return run("readwrite", s => s.put({
        word, blob, fileName: fileName || "", size: blob.size, at: Date.now()
      }));
    },
    get(word) { return run("readonly", s => s.get(word)); },
    allWords() { return run("readonly", s => s.getAllKeys()); },
    clear() { return run("readwrite", s => s.clear()); }
  };
})();

const WordAudio = (() => {
  const known = new Set();
  let wordIndex = {};          // 归一化单词 → 原始单词，用于文件名匹配
  let lastUrl = "";
  let ready = false;

  function setWordList(words) {
    wordIndex = {};
    (words || []).forEach(w => {
      const text = typeof w === "string" ? w : w.word;
      wordIndex[AudioStore.normalize(text)] = text;
    });
  }

  async function refresh() {
    try {
      const keys = await AudioStore.allWords();
      known.clear();
      keys.forEach(k => known.add(k));
    } catch (e) {
      ready = false;
      throw e;
    }
    ready = true;
    return known;
  }

  async function ensureReady() {
    if (!ready) await refresh();
  }

  async function importFiles(files, forceWord) {
    const matched = [];
    const unmatched = [];
    for (const file of files) {
      const stem = file.name.replace(/\.[^.]+$/, "");
      const key = forceWord ? AudioStore.normalize(forceWord) : AudioStore.normalize(stem);
      if (!wordIndex[key]) { unmatched.push(file.name); continue; }
      await AudioStore.put(key, file, file.name);
      known.add(key);
      matched.push(wordIndex[key]);
    }
    if (WordAudio.onChange) WordAudio.onChange({ matched, unmatched });
    return { matched, unmatched };
  }

  async function clearAll() {
    await AudioStore.clear();
    known.clear();
    if (WordAudio.onChange) WordAudio.onChange({ matched: [], unmatched: [] });
  }

  function has(wordText) {
    return known.has(AudioStore.normalize(wordText));
  }

  /** 标准发音优先级：服务器音频 → 本地导入音频 → 浏览器语音合成 */
  async function play(wordObj, opts = {}) {
    await ensureReady();
    const text = typeof wordObj === "string" ? wordObj : wordObj.word;

    if (wordObj && wordObj.audio_url) {
      await Common.playUrl(wordObj.audio_url);
      return { source: "server" };
    }

    const rec = await AudioStore.get(AudioStore.normalize(text));
    if (rec && rec.blob) {
      if (lastUrl) URL.revokeObjectURL(lastUrl);
      lastUrl = URL.createObjectURL(rec.blob);
      const audio = new Audio(lastUrl);
      audio.playbackRate = opts.rate || 1;
      await audio.play();
      return { source: "local", fileName: rec.fileName, audio };
    }

    Common.speak(text, "en-US", opts.rate || 0.9);
    return { source: "tts" };
  }

  return {
    setWordList, refresh, importFiles, clearAll, has, play, known,
    get ready() { return ready; }
  };
})();

// 顶层 const 只存在于脚本的词法作用域，不会自动成为 window 属性；
// 练习页用 window.WordAudio 判断「音频导入脚本是否可用」，这里必须显式导出。
window.AudioStore = AudioStore;
window.WordAudio = WordAudio;

/** 绑定练习页底部的「音频导入」面板
 *
 *  options.revealWord === false 时（听读写），面板里一处都不许出现单词拼写：
 *  答题环节本来不显示拼写，而徽章是按当前单词画的，照抄拼写就等于把答案写在屏幕下方。
 */
function bindWordAudio(getCurrentWord, options) {
  const revealWord = !((options || {}).revealWord === false);
  const input = document.getElementById("audio-input");
  const inputOne = document.getElementById("audio-input-one");
  const status = document.getElementById("audio-status");
  const clearBtn = document.getElementById("audio-clear");
  const badge = document.getElementById("audio-badge");
  if (!input) return;

  function paint(message) {
    if (status) {
      status.textContent = message || `已导入 ${WordAudio.known.size} 个单词的本地音频`;
    }
  }

  function paintBadge() {
    if (!badge) return;
    const w = getCurrentWord();
    if (!w) { badge.textContent = ""; return; }
    // 发音来源有三种，只看本地音频会把服务器的真人音频误报成「合成发音」。
    // 听读写只报来源、不报词：来源本身才是用户要知道的信息。
    const suffix = revealWord ? `：${w.word}` : "";
    if (w.audio_url) {
      badge.className = "badge rounded-pill text-bg-success";
      badge.innerHTML = `<i class="bi bi-headphones me-1"></i>真人音频${suffix}`;
      return;
    }
    const local = WordAudio.has(w.word);
    badge.className = "badge rounded-pill " + (local ? "text-bg-info" : "text-bg-light text-dark border");
    badge.innerHTML = local
      ? `<i class="bi bi-music-note-beamed me-1"></i>本地音频${suffix}`
      : `<i class="bi bi-megaphone me-1"></i>合成发音${revealWord ? `（未导入 ${w.word} 的音频）` : "（本词还没有导入音频）"}`;
  }

  WordAudio.paintBadge = paintBadge;
  WordAudio.onChange = () => { paint(); paintBadge(); };

  input.addEventListener("change", async e => {
    const files = [...e.target.files];
    e.target.value = "";
    if (!files.length) return;
    try {
      const r = await WordAudio.importFiles(files);
      paint(`成功 ${r.matched.length} 个，未匹配 ${r.unmatched.length} 个` +
            (r.unmatched.length ? `：${r.unmatched.join("、")}` : ""));
    } catch (err) {
      paint(`导入失败：${err.message}`);
    }
  });

  if (inputOne) {
    inputOne.addEventListener("change", async e => {
      const w = getCurrentWord();
      const files = [...e.target.files];
      e.target.value = "";
      if (!w || !files.length) return;
      try {
        await WordAudio.importFiles(files.slice(0, 1), w.word);
        paint(`已把「${files[0].name}」绑定到${revealWord ? ` ${w.word}` : "当前单词"}`);
        paintBadge();
      } catch (err) {
        paint(`导入失败：${err.message}`);
      }
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      if (!confirm("确定清除全部已导入的音频吗？")) return;
      try {
        await WordAudio.clearAll();
        paint("已清除全部音频");
        paintBadge();
      } catch (err) {
        paint(`清除失败：${err.message}`);
      }
    });
  }

  WordAudio.refresh()
    .then(() => { paint(); paintBadge(); })
    .catch(err => paint(`音频库不可用：${err.message}`));
}
