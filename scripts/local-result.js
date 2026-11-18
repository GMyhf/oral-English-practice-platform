/* local-result.js · 本地模式的结果页
   服务器模式那张 templates/result.html 是 Jinja 在后台渲染好的；本地模式下服务器上
   一场成绩都没有（后端只给了一个带 id 的空壳页面），所以内容全部由这里从 IndexedDB 取出来现画。

   画出来的结构跟 result.html 一一对应：同样的 1 / 2 号选项卡、同样的逐题折叠、
   同样那四个播放按钮。两种模式共用一套 CSS 和交互，用户看不出自己看的是「另一版」结果页，
   只有顶部那条「本地模式」提示提醒他副本只在手上。

   唯一的实质差别是「我的朗读」：服务器模式给的是一个 URL，本地模式给的是
   "local:<会话 id>/<题号>" 这样的键 —— 录音还在 IndexedDB 里，点播放时才现取、
   现建 object URL。这里做了缓存，同一道题反复听不会反复读 Blob。 */
(() => {
  const $ = id => document.getElementById(id);
  const esc = text => String(text === null || text === undefined ? "" : text)
    .replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const pageData = $("page-data");
  if (!pageData || !window.LocalStore) return;

  const sessionId = Number(pageData.dataset.sessionId);
  const TYPE_NAME = { vowel: "近似音区分", "read-translate": "看读译", "listen-spell": "听读写" };

  // 参数中文名和单位换算跟 app.py 的 RHYTHM_LABELS / SWITCH_LABELS 是同一份口径。
  // 两处都要改的情况很少（加一段节奏时），但既然结果页要照着念给用户听，
  // 就在这里把来源写死在注释里，别让人以为这三行是随手写的字符串。
  const TIME_PARAMS = { round1_ms: "第一轮（秒）", round2_ms: "第二轮（秒）", round_gap_ms: "题间停顿（秒）" };
  const SWITCH_PARAMS = {
    overlap_record: ["第一轮录音方式", { true: "边播边录", false: "先听后录" }],
  };

  const clipUrls = new Map();        // "会话/题号" → object URL
  const missingClips = new Set();    // 已经确认不在了的键，别再重复读一遍库

  function humanize(params) {
    const out = [];
    Object.keys(params || {}).forEach(key => {
      const value = params[key];
      if (TIME_PARAMS[key]) {
        const seconds = Number(value);
        out.push([TIME_PARAMS[key], Number.isFinite(seconds)
          ? String(Math.round(seconds / 100) / 10) : value]);
        return;
      }
      if (SWITCH_PARAMS[key]) {
        const [name, mapping] = SWITCH_PARAMS[key];
        out.push([name, mapping[String(Boolean(value))] !== undefined
          ? mapping[String(Boolean(value))] : String(value)]);
        return;
      }
      out.push([key, String(value)]);
    });
    return out;
  }

  /** 取一题的录音：Blob → object URL，同一个键只建一次 */
  async function clipUrl(key) {
    if (clipUrls.has(key)) return clipUrls.get(key);
    if (missingClips.has(key)) return "";
    const clip = await LocalStore.getRecording(key);
    if (!clip || !clip.blob) {
      missingClips.add(key);
      return "";
    }
    const url = URL.createObjectURL(clip.blob);
    clipUrls.set(key, url);
    return url;
  }

  /** 录音键可能是 "local:12/3"（本地存的），也可能根本是脏数据；只认前者 */
  function localKey(path) {
    return LocalStore.keyOfPath(path) || "";
  }

  function renderVowel(item) {
    const options = item.question.options || [];
    const played = options.find(opt => opt.index === item.question.answer) || {};
    const isSentence = item.question.mode === "sentence";
    const cards = options.map(opt => `
      <div class="col-6">
        <div class="border rounded p-3 text-center ${opt.index === item.question.answer
          ? "border-success bg-success-subtle" : ""}">
          <div class="text-muted small">编号 ${esc(opt.index)}</div>
          <div class="fs-4 fw-bold">${esc(opt.word)}</div>
          ${opt.sentence ? `<div class="small text-body-secondary mt-1">${esc(opt.sentence)}</div>` : ""}
          ${opt.index === item.question.answer
            ? `<div class="small text-success mt-1">${isSentence ? "实际播的是这句" : "实际播放的是这个"}</div>`
            : ""}
        </div>
      </div>`).join("");

    const voice = item.question.voice
      ? `<span class="small text-muted ms-1">${item.question.voice === "f" ? "女声" : "男声"}（每题随机）</span>` : "";

    return `
      <div class="row g-3 mb-3">${cards}</div>
      <p class="mb-1">你的选择：<strong>${item.user_answer === null || item.user_answer === ""
        ? "无回答" : esc(item.user_answer)}</strong>
        · 正确答案：<strong>${esc(item.question.answer)}</strong></p>
      <button class="btn btn-sm btn-outline-primary" data-audio="${esc(item.question.audio_url || "")}">
        <i class="bi bi-volume-up me-1"></i>${isSentence ? "再听一遍句子" : "再听一遍"}
      </button>${voice}`;
  }

  function renderWordRound(item) {
    const q = item.question;
    const type = item.type;
    const key = localKey(item.recording_path);
    const rows = [];
    rows.push(`<tr><th style="width:120px;">单词</th>
      <td class="fs-5 fw-bold text-primary">${esc(q.word)}
        <span class="text-muted fs-6">${esc(q.phonetic || "")}</span></td></tr>`);
    if (type === "read-translate") {
      rows.push(`<tr><th>正确译文</th><td>${esc(q.meaning_cn)}</td></tr>`,
        `<tr><th>你的译文</th><td class="${item.is_correct ? "text-success" : "text-danger"}">${
          esc(item.user_answer || "无回答")}</td></tr>`);
    } else {
      rows.push(`<tr><th>正确拼写</th><td class="fw-semibold">${esc(q.word)}</td></tr>`,
        `<tr><th>你的拼写</th><td class="${item.is_correct ? "text-success" : "text-danger"}">${
          esc(item.user_answer || "无回答")}</td></tr>`,
        `<tr><th>释义</th><td>${esc(q.meaning_cn)}</td></tr>`);
    }
    if (q.example) rows.push(`<tr><th>例句</th><td class="text-muted">${esc(q.example)}</td></tr>`);

    // 播放按钮的三个来源：自己的录音（本地键）、标准发音（服务器静态文件）、两者连着放。
    // 录音不在本地时按钮直接标成不可用，并说明为什么 —— 本地模式最怕的就是
    // 「我明明录了怎么没了」，把原因写在按钮旁边比让他去猜浏览器设置强。
    const mine = key
      ? `<button class="btn btn-sm btn-outline-primary" data-local-key="${esc(key)}">
           <i class="bi bi-mic me-1"></i>我的朗读</button>`
      : `<span class="btn btn-sm btn-outline-secondary disabled">
           <i class="bi bi-mic-mute me-1"></i>没有录到音</span>`;
    const standard = `<button class="btn btn-sm btn-outline-success" data-audio="${esc(q.audio_url || "")}"
        data-say="${esc(q.word || "")}"><i class="bi bi-person-lines-fill me-1"></i>标准发音</button>`;
    const compare = key && q.audio_url
      ? `<button class="btn btn-sm btn-success" data-local-key="${esc(key)}"
           data-then-audio="${esc(q.audio_url)}"><i class="bi bi-sort-down me-1"></i>连续对比</button>`
      : "";
    const voice = q.voice
      ? `<span class="small text-muted align-self-center">${q.voice === "f" ? "女声" : "男声"}（每题随机）</span>` : "";

    return `
      <table class="table table-sm borderless">${rows.join("")}</table>
      <div class="d-flex flex-wrap gap-2">${mine}${standard}${voice}${compare}</div>`;
  }

  function headerText(item) {
    if (item.type === "vowel") {
      const words = (item.question.options || []).map(opt => opt.word).join(" / ");
      return `${esc(words)}${item.question.mode === "sentence" ? "（句子练习）" : ""}`;
    }
    return esc(item.question.word || "");
  }

  async function load() {
    const session = await LocalStore.getSession(sessionId);
    if (!session) {
      $("loading").classList.add("d-none");
      $("missing").classList.remove("d-none");
      return;
    }

    $("loading").classList.add("d-none");
    $("content").classList.remove("d-none");
    $("page-title").textContent = `${TYPE_NAME[session.practice_type] || "练习"} · 练习结果`;
    $("session-time").textContent = session.created_at || "";

    $("stat-total").textContent = session.total;
    $("stat-correct").textContent = session.correct_count;
    $("stat-wrong").textContent = session.wrong_count;
    $("stat-rate").textContent = session.total
      ? `${Math.round(session.correct_count / session.total * 1000) / 10}%` : "-";

    const pairs = humanize(session.params || {});
    if (pairs.length) {
      $("params-line").innerHTML = "练习参数：" + pairs.map(([key, value]) =>
        `<span class="badge text-bg-light border me-1">${esc(key)} = ${esc(value)}</span>`).join("");
    }

    const items = (session.records || []).map(rec => ({
      seq: rec.seq,
      type: (rec.question && rec.question.type) || rec.type || session.practice_type,
      question: rec.question || {},
      user_answer: rec.user_answer,
      is_correct: Boolean(rec.is_correct),
      recording_path: rec.recording_path || "",
    }));

    if (!items.length) {
      $("no-records").classList.remove("d-none");
      return;
    }

    $("review").innerHTML = items.map(item => `
      <div class="accordion-item">
        <h2 class="accordion-header">
          <button class="accordion-button ${item.seq === items[0].seq ? "" : "collapsed"}" type="button"
                  data-bs-toggle="collapse" data-bs-target="#q${esc(item.seq)}">
            <span class="badge me-2 ${item.is_correct ? "text-bg-success" : "text-bg-danger"}">
              ${item.is_correct ? "✓" : "✗"}</span>
            第 ${esc(item.seq)} 题 · ${headerText(item)}
          </button>
        </h2>
        <div id="q${esc(item.seq)}" class="accordion-collapse collapse ${item.seq === items[0].seq ? "show" : ""}">
          <div class="accordion-body">
            ${item.type === "vowel" ? renderVowel(item) : renderWordRound(item)}
          </div>
        </div>
      </div>`).join("");

    bindPlayback();
  }

  function bindPlayback() {
    document.querySelectorAll("[data-audio],[data-local-key]").forEach(button => {
      button.addEventListener("click", async () => {
        const key = button.dataset.localKey || "";
        let url = button.dataset.audio || "";
        if (key) {
          url = await clipUrl(key);
          if (!url) {
            Common.toast("这道题的录音不在这台设备上了 —— 本地模式的录音只存在浏览器里，" +
              "清除浏览数据或换设备就会看不到", "warning", 6000);
            return;
          }
        }
        if (!url) {
          if (button.dataset.say) Common.speak(button.dataset.say);
          else Common.toast("这道题没有可播放的音频");
          return;
        }
        try {
          await Common.playUrl(url, () => {
            if (button.dataset.thenAudio) Common.playUrl(button.dataset.thenAudio);
          });
        } catch (err) {
          if (button.dataset.say) Common.speak(button.dataset.say);
          else Common.toast("音频播放失败", "warning");
        }
      });
    });
  }

  // 页面离开时回收 object URL：只建了几个的话无所谓，但一整场 100 题全听一遍
  // 就是 100 个 Blob 引用挂着，手机上等不到下一次 GC。
  window.addEventListener("pagehide", () => {
    clipUrls.forEach(url => URL.revokeObjectURL(url));
    clipUrls.clear();
  });

  load().catch(err => {
    $("loading").classList.add("d-none");
    $("missing").classList.remove("d-none");
    $("missing").querySelector(".small").innerHTML =
      `读取本机记录时出错：${esc(err.message)}。` +
      "如果提示被其它标签页占用，请关掉本站其它页面后刷新。";
  });
})();
