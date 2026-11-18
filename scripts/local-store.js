/* local-store.js · 本地模式的数据落地（浏览器 IndexedDB）
   只在 APP_MODE=local 时由 base.html 加载；服务器模式下一个字节都不会跑到这里。

   存的东西和服务器模式一一对应，这样结果页和导出文件在两种模式下形状一致：
     sessions  一次练习（对应 practice_sessions）
     records   逐题作答（对应 practice_records，主键是 [session_id, seq]）
     audio     逐题朗读录音（对应 data/recordings/ 里那些文件，值直接是 Blob）
     meta      备份时间之类的杂项

   为什么不用 localStorage：录音是二进制，一次 20 题的练习就是 20 个 Blob。
   localStorage 只能存字符串、总量约 5MB，把 Blob 转 base64 塞进去，一次练习就把它顶穿了。
   IndexedDB 能直接存 Blob，配额按磁盘空闲算。
   本仓库已有同样的先例：scripts/audio-store.js 就是用 IndexedDB 存用户导入的单词音频。

   ⚠️ 一条一定要知道的性质：默认情况下浏览器可以随时清掉 IndexedDB —— 磁盘紧张、
   用户清浏览数据、隐私模式、站点长期不用被判定为可回收，都会丢。所以本地模式做了两件事兜底：
     1) 申请持久化存储（requestPersistence），批准后浏览器不再自动驱逐；
     2) 数据页提供导出 / 导入备份，换设备或重装系统前自己留一份。
   两条都摆在「我的数据」页上，不用等人踩了坑再来问。 */

const LocalStore = (() => {
  const DB_NAME = "ep_local_db";
  const VERSION = 1;
  const SESSIONS = "sessions";
  const RECORDS = "records";
  const AUDIO = "audio";
  const META = "meta";
  const STORE_NAMES = [SESSIONS, RECORDS, AUDIO, META];

  const KIND = "ep-local-backup";
  const BACKUP_VERSION = 1;

  // 导出带录音时，原始音频总量超过这个值就只导记录并说明原因：
  // base64 让体积涨三分之一，再把整份 JSON 拼成字符串，手机上这一步就是几百 MB 内存。
  const MAX_EXPORT_AUDIO_BYTES = 40 * 1024 * 1024;

  // 结果页和导出记录里的录音路径前缀。服务器模式是 "data/recordings/u1_ab12cd34.webm"，
  // 本地模式是 "local:12/3"（12 号会话第 3 题）—— 一眼能分辨该找 IndexedDB 还是找磁盘。
  const KEY_PREFIX = "local:";

  let dbPromise = null;

  function open() {
    if (!dbPromise) {
      dbPromise = new Promise((resolve, reject) => {
        if (!window.indexedDB) {
          reject(new Error("这台设备的浏览器不支持 IndexedDB，本地模式存不了数据"));
          return;
        }
        const req = indexedDB.open(DB_NAME, VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          // 每张表单独判断：以后加表时升级不会动已有的数据
          if (!db.objectStoreNames.contains(SESSIONS)) {
            db.createObjectStore(SESSIONS, { keyPath: "id", autoIncrement: true });
          }
          if (!db.objectStoreNames.contains(RECORDS)) {
            const store = db.createObjectStore(RECORDS, { keyPath: ["session_id", "seq"] });
            // 按会话取整场成绩、删会话时连带清题，都走这个索引
            store.createIndex("by_session", "session_id");
          }
          if (!db.objectStoreNames.contains(AUDIO)) {
            const store = db.createObjectStore(AUDIO, { keyPath: "key" });
            store.createIndex("by_session", "session_id");
          }
          if (!db.objectStoreNames.contains(META)) {
            db.createObjectStore(META, { keyPath: "k" });
          }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error || new Error("打不开本地数据库"));
        // 另一个标签页拿着旧版本不放：说清该做什么，别让人对着 UnknownError 猜
        req.onblocked = () =>
          reject(new Error("本地数据库被其它标签页占用，请关闭本站其它页面后重试"));
      });
      dbPromise.catch(() => { dbPromise = null; });   // 失败的 promise 不缓存
    }
    return dbPromise;
  }

  function once(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error("本地数据库读写失败"));
    });
  }

  /** 一个事务跑完 body，complete 才算写成功。
   *  body 里可以 await，但只能 await IndexedDB 请求：await 一个 setTimeout 或 fetch
   *  会让事务提前关闭，之后的写入静默失败 —— 那正是「练完 20 题、结果页是空的」的形状。 */
  async function transact(mode, body) {
    const db = await open();
    const tx = db.transaction(STORE_NAMES, mode);
    const stores = {
      sessions: tx.objectStore(SESSIONS),
      records: tx.objectStore(RECORDS),
      audio: tx.objectStore(AUDIO),
      meta: tx.objectStore(META),
    };
    const result = await body(stores);
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve(result);
      tx.onerror = () => reject(tx.error || new Error("本地写入失败"));
      tx.onabort = () => reject(tx.error || new Error("本地写入被中断（多半是存储配额满了）"));
    });
  }

  function audioKey(sessionId, seq) {
    return `${Number(sessionId)}/${Number(seq)}`;
  }

  /** 把记录里的 recording_path 还原成 IndexedDB 的键；不是本地格式返回 null */
  function keyOfPath(path) {
    const text = String(path || "");
    return text.startsWith(KEY_PREFIX) ? text.slice(KEY_PREFIX.length) : null;
  }

  function pathOfKey(sessionId, seq) {
    return KEY_PREFIX + audioKey(sessionId, seq);
  }

  /** 本地模式没有服务器时钟可参考，直接用这台设备的时间，和浏览器里看到的一致 */
  function nowLabel(ts) {
    const date = ts ? new Date(ts) : new Date();
    const pad = n => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
      `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  // ---------------------------------------------------------------- 写

  /** 返回自增 id（整数），所以结果页可以继续用 /result/<int:id> 这条路由 */
  async function createSession(row) {
    const session = {
      practice_type: row.practice_type,
      params: row.params || {},
      total: 0,
      correct_count: 0,
      wrong_count: 0,
      finished: 0,
      created_at: nowLabel(),
      ts: Date.now(),
    };
    return transact("readwrite", stores => once(stores.sessions.add(session)));
  }

  async function putRecording(sessionId, seq, blob, ext) {
    const sid = Number(sessionId);
    const key = audioKey(sid, seq);
    await transact("readwrite", stores =>
      once(stores.audio.put({
        key,
        session_id: sid,
        seq: Number(seq),
        blob,
        ext: ext || "webm",
        mime: (blob && blob.type) || "",
        size: (blob && blob.size) || 0,
        at: Date.now(),
      })));
    return pathOfKey(sid, seq);
  }

  /** 整场练习的作答一次事务写完：中途出错整批回滚，
   *  不会留下「存了 7 题、结果页第 8 题起空白」的半截成绩。 */
  async function saveRecords(sessionId, records) {
    const sid = Number(sessionId);
    await transact("readwrite", async stores => {
      for (const rec of records || []) {
        // 有录音就写成带前缀的 local:会话/题号 —— 结果页、导出导入全靠 keyOfPath() 认这个
        // 前缀，写成裸键「2/1」的话音频明明在库裡，页面却显示「没有录到音」
        // （11-18 浏览器实测抓到的，当时就是这里把前缀弄丢了）。
        // 不直接抄前端传来的字符串：按本场的 sid/seq 重拼，脏值和别人的会话路径在这儿断掉。
        const hasClip = keyOfPath(rec.recording_path) !== null || Boolean(rec.recording_path);
        await once(stores.records.put({
          session_id: sid,
          seq: Number(rec.seq),
          question: rec.question || {},
          // 和后端一致：只留标量文本，截到 200 字符，免得有人把对象塞进答案
          user_answer: rec.user_answer === null || rec.user_answer === undefined
            ? null : String(rec.user_answer).slice(0, 200),
          is_correct: rec.is_correct ? 1 : 0,
          recording_path: hasClip ? pathOfKey(sid, rec.seq) : null,
        }));
      }
    });
    return records.length;
  }

  async function finishSession(sessionId, stats) {
    const sid = Number(sessionId);
    await transact("readwrite", async stores => {
      const row = await once(stores.sessions.get(sid));
      if (!row) throw new Error(`本机找不到会话 #${sid}`);
      row.total = Number(stats.total) || 0;
      row.correct_count = Number(stats.correct) || 0;
      row.wrong_count = Number(stats.wrong) || 0;
      row.finished = 1;
      row.finished_at = nowLabel();
      await once(stores.sessions.put(row));
    });
    return sid;
  }

  // ---------------------------------------------------------------- 读

  async function getSession(sessionId) {
    const sid = Number(sessionId);
    return transact("readonly", async stores => {
      const session = await once(stores.sessions.get(sid));
      if (!session) return null;
      const records = await once(stores.records.index("by_session").getAll(IDBKeyRange.only(sid)));
      return { ...session, records: records.sort((a, b) => a.seq - b.seq) };
    });
  }

  /** id 是 autoIncrement，越新越大，所以按 id 倒序就是「最近练习」 */
  async function listSessions(limit = 50) {
    const rows = await transact("readonly", stores => once(stores.sessions.getAll()));
    const sorted = rows.sort((a, b) => b.id - a.id);
    return limit > 0 ? sorted.slice(0, limit) : sorted;
  }

  async function getRecording(key) {
    return transact("readonly", stores => once(stores.audio.get(key)));
  }

  /** 每场练习各有多少条录音，数据页的列表要用。
   *  一次遍历全表，比逐场 count 少几十次事务往返。 */
  async function clipCountBySession() {
    const clips = await transact("readonly", stores => once(stores.audio.getAll()));
    const counts = {};
    clips.forEach(clip => {
      counts[clip.session_id] = (counts[clip.session_id] || 0) + 1;
    });
    return counts;
  }

  async function summary() {
    return transact("readonly", async stores => {
      const [sessions, records, clips, sizes] = await Promise.all([
        once(stores.sessions.count()),
        once(stores.records.count()),
        once(stores.audio.count()),
        once(stores.audio.getAll()),
      ]);
      // 只累加 size 这个字段，不去动 Blob 本身：getAll 拿到 Blob 引用不占额外内存
      const bytes = sizes.reduce((sum, item) => sum + (item.size || 0), 0);
      return { sessions, records, clips, bytes };
    });
  }

  // ---------------------------------------------------------------- 删

  async function deleteSession(sessionId) {
    const sid = Number(sessionId);
    return transact("readwrite", async stores => {
      await once(stores.sessions.delete(sid));
      const recordKeys = await once(
        stores.records.index("by_session").getAllKeys(IDBKeyRange.only(sid)));
      for (const key of recordKeys) await once(stores.records.delete(key));
      const clipKeys = await once(
        stores.audio.index("by_session").getAllKeys(IDBKeyRange.only(sid)));
      for (const key of clipKeys) await once(stores.audio.delete(key));
      return recordKeys.length;
    });
  }

  async function clearAll() {
    await transact("readwrite", async stores => {
      await Promise.all([
        once(stores.sessions.clear()),
        once(stores.records.clear()),
        once(stores.audio.clear()),
      ]);
    });
    // 设置（localStorage 里的 ep_settings）故意不清：清空数据是删练习记录，
    // 把人家调好的时长一起抹掉是另一回事，得让人明确去设置页操作。
  }

  // ---------------------------------------------------------------- 配额 / 持久化

  async function estimate() {
    if (!navigator.storage || !navigator.storage.estimate) return null;
    const info = await navigator.storage.estimate();
    return { usage: info.usage || 0, quota: info.quota || 0 };
  }

  async function persistence() {
    if (!navigator.storage || !navigator.storage.persisted) return null;
    return navigator.storage.persisted();
  }

  /** 申请「别自动驱逐我的数据」。多数桌面浏览器在用户有交互时会自动批准，
   *  手机 Chrome 要用户在权限弹窗上点允许，被拒了也不算错误 —— 只能提示备份。 */
  async function requestPersistence() {
    if (!navigator.storage || !navigator.storage.persist) {
      return { ok: false, reason: "这台设备的浏览器不支持持久化存储，请务必定期导出备份" };
    }
    // 已经授予过就不要再调一次 persist()：重复调用在某些浏览器上会被直接拒
    const current = await persistence();
    if (current === true) return { ok: true, already: true };
    const granted = await navigator.storage.persist();
    return { ok: Boolean(granted), reason: granted ? "" : "浏览器没有批准持久化存储请求" };
  }

  // ---------------------------------------------------------------- 备份：导出 / 导入

  function blobToDataURL(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error || new Error("录音读取失败"));
      reader.readAsDataURL(blob);
    });
  }

  /** data URL → Blob。不用 fetch(dataUrl)：老一截 Safari 上那条路不通，
   *  而且这里本来就是纯字符串运算，手写反而更可控。 */
  function dataURLToBlob(url) {
    const text = String(url || "");
    const comma = text.indexOf(",");
    if (!text.startsWith("data:") || comma < 0) return null;
    const head = text.slice(5, comma);
    const body = text.slice(comma + 1);
    const mime = (head.split(";")[0] || "application/octet-stream");
    const isBase64 = /;base64/i.test(head);
    let bytes;
    try {
      if (isBase64) {
        const bin = atob(body);
        bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      } else {
        bytes = new TextEncoder().encode(decodeURIComponent(body));
      }
    } catch (err) {
      return null;
    }
    return new Blob([bytes], { type: mime });
  }

  /** options.includeAudio 为真时把录音一起打包（data URL 形式）。
   *  返回 {text, filename, withAudio, skippedAudio, reason, size}
   *
   *  读取集中在一个只读事务里做完，之后的 base64 转换都在事务外 ——
   *  FileReader 不是 IndexedDB 请求，在事务里 await 它会把事务关掉，
   *  后面接着写的代码就会静默失效（这个坑见上面 transact 的注释）。 */
  async function exportBackup(options = {}) {
    const wantAudio = options.includeAudio !== false;
    const dump = await transact("readonly", async stores => ({
      sessions: await once(stores.sessions.getAll()),
      records: await once(stores.records.getAll()),
      clips: await once(stores.audio.getAll()),
    }));

    const bySession = new Map();
    dump.records.forEach(rec => {
      const list = bySession.get(rec.session_id) || [];
      list.push(rec);
      bySession.set(rec.session_id, list);
    });

    const payload = dump.sessions
      .slice()
      .sort((a, b) => a.id - b.id)
      .map(session => ({
        ...session,
        records: (bySession.get(session.id) || []).sort((a, b) => a.seq - b.seq),
      }));

    const clipCount = dump.clips.length;
    const audioBytes = dump.clips.reduce((sum, clip) => sum + (clip.size || 0), 0);

    let withAudio = false;
    let skippedReason = "";
    if (!clipCount) {
      skippedReason = "";                                   // 本来就没录音，不算「跳过」
    } else if (!wantAudio) {
      skippedReason = "按你的选择只导出练习记录（不含录音）";
    } else if (audioBytes > MAX_EXPORT_AUDIO_BYTES) {
      skippedReason = `录音合计 ${(audioBytes / 1048576).toFixed(1)}MB，` +
        `超过单次备份上限 ${(MAX_EXPORT_AUDIO_BYTES / 1048576).toFixed(0)}MB，本次只导出练习记录`;
    } else {
      withAudio = true;
    }

    const audio = {};
    if (withAudio) {
      for (const clip of dump.clips) {
        if (!clip.blob) continue;
        audio[clip.key] = await blobToDataURL(clip.blob);
      }
    }

    const doc = {
      kind: KIND,
      version: BACKUP_VERSION,
      app: "oral-english-practice-platform",
      stored_in: "browser",
      exported_at: nowLabel(),
      // 记一下是从哪个站点导出的：拿到另一台上去导入时先给用户看一眼，
      // 免得把 A 班机房的记录灌进 B 班那台里、还以为自己导错了
      origin: location.host,
      counts: {
        sessions: payload.length,
        records: payload.reduce((sum, s) => sum + (s.records || []).length, 0),
        audio_clips: withAudio ? clipCount : 0,
      },
      sessions: payload,
      audio: withAudio ? audio : {},
      settings: readSettings(),
    };
    const text = JSON.stringify(doc, null, withAudio ? undefined : 2);
    const stamp = new Date();
    const pad = n => String(n).padStart(2, "0");
    return {
      text,
      size: text.length,
      withAudio,
      skippedAudio: Boolean(clipCount) && !withAudio,
      reason: skippedReason,
      counts: doc.counts,
      filename: `练习记录备份${withAudio ? "-含录音" : ""}-` +
        `${stamp.getFullYear()}${pad(stamp.getMonth() + 1)}${pad(stamp.getDate())}` +
        `-${pad(stamp.getHours())}${pad(stamp.getMinutes())}.json`,
    };
  }

  /** 导入是「追加」，不是「覆盖」：每条会话都拿本机新的自增 id 落库。
   *  原因是自增 id 在导出文件和当前库里都会从 1 开始，直接写会互相踩；
   *  而备份的意义就是不许把已经练过的成绩冲掉。 */
  async function importBackup(text) {
    let doc;
    try {
      doc = JSON.parse(text);
    } catch (err) {
      throw new Error("文件不是合法的 JSON，无法导入");
    }
    if (!doc || doc.kind !== KIND) throw new Error("这不是本平台导出的备份文件");
    if (Number(doc.version) > BACKUP_VERSION) {
      throw new Error(`备份文件的版本是 ${doc.version}，这台设备上的程序只认到 ${BACKUP_VERSION}，请先更新页面`);
    }
    const sessions = Array.isArray(doc.sessions) ? doc.sessions : [];
    if (!sessions.length) return { sessions: 0, records: 0, audio_clips: 0 };

    const audio = doc.audio && typeof doc.audio === "object" ? doc.audio : {};
    let addedRecords = 0;
    let addedClips = 0;

    const newIds = await transact("readwrite", async stores => {
      const ids = [];
      for (const item of sessions) {
        if (!item || !item.practice_type) continue;      // 缺类型的条目直接丢，别写进库里当垃圾
        const session = {
          practice_type: String(item.practice_type),
          params: item.params && typeof item.params === "object" ? item.params : {},
          total: Number(item.total) || 0,
          correct_count: Number(item.correct_count) || 0,
          wrong_count: Number(item.wrong_count) || 0,
          finished: item.finished ? 1 : 0,
          created_at: item.created_at || nowLabel(),
          ts: Number(item.ts) || Date.now(),
          imported_from: item.id,          // 备份文件里原来的 id，只用于排查
        };
        const newId = await once(stores.sessions.add(session));
        ids.push({ oldId: Number(item.id), newId });

        for (const rec of item.records || []) {
          const seq = Number(rec.seq);
          if (!Number.isFinite(seq)) continue;
          const oldKey = keyOfPath(rec.recording_path);
          await once(stores.records.put({
            session_id: newId,
            seq,
            question: rec.question && typeof rec.question === "object" ? rec.question : {},
            user_answer: rec.user_answer === undefined ? null : String(rec.user_answer).slice(0, 200),
            is_correct: rec.is_correct ? 1 : 0,
            recording_path: oldKey ? pathOfKey(newId, seq) : null,
          }));
          addedRecords++;

          const clip = oldKey ? audio[oldKey] : null;
          if (clip) {
            const blob = dataURLToBlob(clip);
            if (blob) {
              await once(stores.audio.put({
                key: audioKey(newId, seq),
                session_id: newId,
                seq,
                blob,
                ext: (blob.type || "").split("/").pop() || "webm",
                mime: blob.type || "",
                size: blob.size,
                at: Date.now(),
              }));
              addedClips++;
            }
          }
        }
      }
      return ids;
    });

    if (doc.settings && typeof doc.settings === "object") writeSettings(doc.settings);
    return {
      sessions: newIds.length,
      records: addedRecords,
      audio_clips: addedClips,
      // 带录音的备份导入后同样受「浏览器可能驱逐」这条约束，提示一句比事后解释好
      hint: Object.keys(audio).length ? "录音已一并导入" : "本文件不含录音",
    };
  }

  async function lastBackup() {
    return transact("readonly", stores => once(stores.meta.get("last_backup")));
  }

  async function markBackup(filename, withAudio, size) {
    await transact("readwrite", stores =>
      once(stores.meta.put({ k: "last_backup", at: nowLabel(), filename, withAudio, size })));
  }

  // ---------------------------------------------------------------- 本机设置（localStorage）
  // 设置是几个数字和一个开关，量小到不构成 IndexedDB 的理由；
  // 反过来用 localStorage 有个实打实的好处：练习页在脚本加载的第一拍就能同步读到，
  // 不用等 IndexedDB 打开完再回填输入框。

  const SETTINGS_KEY = "ep_settings";

  const DEFAULT_SETTINGS = {
    default_count: 20,
    time_limit: 1,
    round1_seconds: 1.5,
    round2_seconds: 5,
    round_gap: 0.2,
    overlap_record: true,
  };

  function readSettings() {
    const out = { ...DEFAULT_SETTINGS };
    try {
      const raw = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}");
      Object.keys(DEFAULT_SETTINGS).forEach(key => {
        if (raw[key] === undefined || raw[key] === null) return;
        out[key] = raw[key];
      });
    } catch (err) {
      // 隐私模式下 localStorage 可能整个不可用，或者被人手改坏了：回默认值而不是抛
    }
    return out;
  }

  function writeSettings(patch) {
    const next = { ...readSettings(), ...(patch || {}) };
    try {
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(next));
    } catch (err) {
      // 写失败只在调用方形态下有意义，交给上层提示
    }
    return next;
  }

  function settingsAvailable() {
    try {
      const probe = "__ep_probe__";
      localStorage.setItem(probe, "1");
      localStorage.removeItem(probe);
      return true;
    } catch (err) {
      return false;
    }
  }

  return {
    DB_NAME, VERSION, KIND, BACKUP_VERSION, KEY_PREFIX, MAX_EXPORT_AUDIO_BYTES,
    open, createSession, putRecording, saveRecords, finishSession,
    getSession, listSessions, getRecording, summary, clipCountBySession, deleteSession, clearAll,
    audioKey, keyOfPath, pathOfKey, nowLabel,
    estimate, persistence, requestPersistence,
    exportBackup, importBackup, lastBackup, markBackup,
    dataURLToBlob, blobToDataURL,
    readSettings, writeSettings, settingsAvailable, DEFAULT_SETTINGS, SETTINGS_KEY,
  };
})();

// 顶层 const 只存在于脚本的词法作用域，不会自动成为 window 属性；
// practice-store.js 和页面脚本都要用 window.LocalStore 判断「本地存储层在不在」，
// 这一行是必须的（同一个坑见 scripts/audio-store.js 末尾的注释）。
window.LocalStore = LocalStore;
