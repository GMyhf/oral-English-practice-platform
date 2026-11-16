/* tworounds.js · 看读译 / 听读写的两轮练习引擎
   两个页面共用本脚本，靠 <body> 上的 data-practice-type 区分行为：

   read-translate（看读译）
     第一轮：屏幕展示单词，同时录音用户朗读
     第二轮：屏幕展示单词，键入中文译文（任一项即可）
   listen-spell（听读写）
     第一轮：播放单词音频后录音跟读（不显示拼写）
     第二轮：再播一次音频，限时内键入正确拼写

   三段时长——第一轮、第二轮、题目之间的停顿——都从练习设置里的输入框读，
   默认值和 min/max 由 config.py 经 app.py 渲染进输入框，这里只负责按边界夹一遍；
   点「开始练习」时读一次并写进会话 params，中途改框不影响正在跑的这轮。

   结束后把所有作答一次性提交 /api/practice/finish，跳转结果页逐题回看。 */
(() => {
  const body = document.body;
  const practiceType = body.dataset.practiceType;
  if (practiceType !== "read-translate" && practiceType !== "listen-spell") return;

  const $ = id => document.getElementById(id);

  const CFG = {
    "read-translate": {
      showWordRound1: true,
      showWordRound2: true,
      playAudioRound1: false,
      playAudioRound2: false,
      inputPlaceholder: "输入这个单词的中文译文（任写一项）",
      emptyAnswer: "无回答",
      roundNote: seconds => `展示 ${seconds} 秒，超时未提交记为无回答`,
      judge: judgeChinese
    },
    "listen-spell": {
      showWordRound1: false,
      showWordRound2: false,
      playAudioRound1: true,
      playAudioRound2: true,
      inputPlaceholder: "输入你听到的单词拼写",
      emptyAnswer: "无回答",
      roundNote: seconds => `播放后 ${seconds} 秒内提交，超时记为无回答`,
      judge: (word, answer) => answer.trim().toLowerCase() === word.word.toLowerCase()
    }
  };
  const cfg = CFG[practiceType];

  // 输入框不在的时候（比如别处复用这段脚本）退回以前写死的那三个值
  const FALLBACK_SECONDS = { round1_seconds: 1, round2_seconds: 5, round_gap: 0.2 };
  let round1Ms = 1000;
  let round2Ms = 5000;
  let gapMs = 200;

  let sessionId = null;
  let words = [];
  let records = [];
  let recordings = {};        // seq → {path, url}
  let stats = { right: 0, wrong: 0 };
  let recorder = null;
  let micBroken = false;
  let currentWord = null;

  /** 读一个秒数输入框：清空或写非数字都用默认值兜底，出格的值夹到 min/max。 */
  function readSeconds(id) {
    const el = $(id);
    if (!el) return FALLBACK_SECONDS[id];
    const raw = String(el.value).trim();
    const parsed = Number(raw);
    let seconds = raw === "" || !Number.isFinite(parsed) ? Number(el.getAttribute("value")) : parsed;
    if (!Number.isFinite(seconds)) seconds = FALLBACK_SECONDS[id];
    const lo = Number(el.min), hi = Number(el.max);
    if (Number.isFinite(lo)) seconds = Math.max(lo, seconds);
    if (Number.isFinite(hi)) seconds = Math.min(hi, seconds);
    return seconds;
  }

  // ---------------------------------------------------------------- 开始

  $("btn-start").addEventListener("click", async () => {
    const count = Number($("count").value) || 20;
    const seconds = {
      round1: readSeconds("round1_seconds"),
      round2: readSeconds("round2_seconds"),
      gap: readSeconds("round_gap")
    };
    round1Ms = Math.round(seconds.round1 * 1000);
    round2Ms = Math.round(seconds.round2 * 1000);
    gapMs = Math.round(seconds.gap * 1000);
    const note = $("round2-note");
    if (note && cfg.roundNote) note.textContent = cfg.roundNote(seconds.round2);
    $("btn-start").disabled = true;
    try {
      const sess = await Common.api("/api/practice/session", {
        practice_type: practiceType,
        params: {
          count,
          round1_ms: round1Ms,
          round2_ms: round2Ms,
          round_gap_ms: gapMs
        }
      });
      sessionId = sess.session_id;

      const data = await Common.api("/api/words", { count });
      words = data.words;
      if (!words.length) throw new Error("词库里没有单词，请先运行 scripts/init_db.py");
      if (data.actual < data.requested) {
        // 以前这里会把用户选的 100 题静默变成 30 题，现在如实说明
        Common.toast(`词库里只有 ${data.bank_size} 个单词，本次按 ${data.actual} 题进行`, "warning", 4200);
      }

      // 浏览器语音列表是异步加载的，不预热的话第一个单词的合成发音完全无声
      await Common.warmUpSpeech();

      if (window.WordAudio) {
        WordAudio.setWordList(words);
        await WordAudio.refresh().catch(() => {});
      }
      warnIfNoAudioSource();

      $("setup").classList.add("d-none");
      $("stage").classList.remove("d-none");
      await runRound1();
    } catch (err) {
      Common.toast(err.message, "danger");
      $("btn-start").disabled = false;
    }
  });

  // ---------------------------------------------------------------- 第一轮：录音

  async function runRound1() {
    setStageTitle(`第一轮 · 朗读（每词 ${round1Ms / 1000} 秒，共 ${words.length} 个词）`);
    setPane("round1");
    // 第一轮不显示作答框，先把它们置为不可用，避免"看不见但仍可输入"的状态
    $("answer-input").disabled = true;
    $("answer-submit").disabled = true;
    for (let i = 0; i < words.length; i++) {
      const word = words[i];
      const seq = i + 1;
      setProgress(seq, words.length, "请大声朗读");
      showWord(word, cfg.showWordRound1);
      setTimerBar(0);

      if (cfg.playAudioRound1) await playStandard(word);

      const blobInfo = await recordFor(round1Ms);
      if (blobInfo) {
        try {
          const saved = await uploadRecording(blobInfo.blob, blobInfo.ext, {
            session_id: sessionId, seq
          });
          recordings[seq] = { path: saved.path, url: saved.url };
        } catch (err) {
          /* 录音上传失败不影响判分，结果页会显示「没有录到音」 */
        }
      }
      await Common.wait(gapMs);
    }
    await runRound2();
  }

  async function recordFor(ms) {
    if (micBroken || !Recorder.supported()) {
      if (!micBroken) {
        micBroken = true;
        Common.toast("麦克风不可用，本次练习跳过录音，只判答题目", "warning");
      }
      await timedBar(ms);
      return null;
    }
    try {
      recorder = new Recorder();
      await recorder.start();
      $("rec-dot").classList.remove("d-none");
      $("rec-dot").classList.add("recording");
      await timedBar(ms);
      const result = await recorder.stop();
      return result.blob && result.blob.size > 0 ? result : null;
    } catch (err) {
      micBroken = true;
      Common.toast(`无法录音（${err.message}），后续题目跳过录音`, "warning");
      await timedBar(ms);
      return null;
    } finally {
      const dot = $("rec-dot");
      dot.classList.add("d-none");
      dot.classList.remove("recording");
    }
  }

  // ---------------------------------------------------------------- 第二轮：作答

  async function runRound2() {
    setStageTitle(`第二轮 · 作答（每题 ${round2Ms / 1000} 秒，共 ${words.length} 个词）`);
    setPane("round2");
    for (let i = 0; i < words.length; i++) {
      const word = words[i];
      const seq = i + 1;
      setProgress(seq, words.length, "输入答案");
      showWord(word, cfg.showWordRound2);
      $("answer-input").value = "";
      $("answer-input").placeholder = cfg.inputPlaceholder;
      $("answer-input").disabled = false;
      $("answer-submit").disabled = false;
      $("answer-input").focus();

      let submitted = null;
      let done = false;
      let timer = null;
      const finishEarly = () => {
        if (done) return;
        done = true;
        submitted = $("answer-input").value;
        if (timer) timer.stop(true);
      };
      $("answer-submit").onclick = finishEarly;
      $("answer-input").onkeydown = e => { if (e.key === "Enter") finishEarly(); };

      if (cfg.playAudioRound2) await playStandard(word);

      const bar = $("round2-timer");
      timer = Common.startCountdown(round2Ms / 1000, left => {
        const ratio = Math.max(0, Math.min(1, left / (round2Ms / 1000)));
        bar.style.width = `${ratio * 100}%`;
        bar.classList.toggle("urgent", ratio < 0.3);
      });
      if (done) timer.stop(true);   // 用户在播放音频期间就已提交，不再等满整段限时
      await timer.promise;

      const answer = (submitted === null ? "" : submitted).trim();
      const correct = answer !== "" && cfg.judge(word, answer);
      if (correct) stats.right++; else stats.wrong++;
      setProgress(seq, words.length, correct ? "✓ 正确" : "✗ 错误");

      records.push({
        seq,
        type: practiceType,
        question: {
          type: practiceType,
          word: word.word,
          phonetic: word.phonetic,
          meaning_cn: word.meaning_cn,
          example: word.example,
          audio_url: word.audio_url || null
        },
        user_answer: answer || cfg.emptyAnswer,
        is_correct: correct,
        recording_path: (recordings[seq] || {}).path || null
      });

      // 一题判完就让输入框和提交按钮失去响应：第二轮中间的 250ms 空隙、
      // 以及回跳到第一轮时，看不见的输入框不应该还能被键盘敲进去
      $("answer-submit").onclick = null;
      $("answer-input").onkeydown = null;
      $("answer-input").disabled = true;
      $("answer-submit").disabled = true;
      await Common.wait(gapMs);
    }
    await finish();
  }

  function judgeChinese(word, answer) {
    const senses = String(word.meaning_cn || "").split(/[；;／/，,、]/).map(s => s.trim()).filter(Boolean);
    const text = answer.trim();
    if (!text) return false;
    return senses.some(sense => sense === text || sense.includes(text) || text.includes(sense));
  }

  // ---------------------------------------------------------------- 辅助

  /** 听读写全程不显示拼写，一旦三种发音来源都不可用，用户听到的就是静音 */
  function warnIfNoAudioSource() {
    if (!(cfg.playAudioRound1 || cfg.playAudioRound2)) return;
    const hasServerAudio = words.some(w => w.audio_url);
    const hasLocalAudio = window.WordAudio && WordAudio.known.size > 0;
    if (hasServerAudio || hasLocalAudio || Common.speechReady()) return;
    Common.toast(
      "这台设备上既没有这些单词的音频，浏览器也没有可用的英文语音，听读写会听不到声音。" +
      "请用下方「音频导入」上传单词音频，或换一台装有英文语音的设备。", "danger", 9000);
  }

  async function playStandard(word) {
    try {
      if (window.WordAudio) await WordAudio.play(word);
      else if (word.audio_url) await Common.playUrl(word.audio_url);
      else Common.speak(word.word);
    } catch (err) {
      Common.speak(word.word);
    }
  }

  function showWord(word, visible) {
    currentWord = word;
    if (window.WordAudio && WordAudio.paintBadge) WordAudio.paintBadge();

    const box = $("word-box");
    if (!visible) {
      box.textContent = "🔒 不显示拼写";
      box.classList.add("text-muted");
      $("phonetic-box").textContent = "";
      return;
    }
    box.classList.remove("text-muted");
    box.textContent = word.word;
    $("phonetic-box").textContent = word.phonetic || "";
  }

  function setStageTitle(text) { $("stage-title").textContent = text; }

  function setPane(name) {
    document.querySelectorAll("[data-pane]").forEach(el => {
      el.classList.toggle("d-none", el.dataset.pane !== name);
    });
  }

  function setProgress(seq, total, hint) {
    $("progress").textContent = `第 ${seq} / ${total} 题`;
    $("score").textContent = `对 ${stats.right} · 错 ${stats.wrong}`;
    $("hint").textContent = hint;
  }

  function setTimerBar(ratio) {
    $("round1-bar").style.width = `${ratio * 100}%`;
  }

  async function timedBar(ms) {
    const started = Date.now();
    return new Promise(resolve => {
      // 用 setInterval 而不是 requestAnimationFrame：标签页切到后台时 rAF 会完全停摆，
      // 练习就卡在正在录音的那一个单词上。
      const id = setInterval(() => {
        const left = ms - (Date.now() - started);
        setTimerBar(Math.max(0, left / ms));
        if (left <= 0) {
          clearInterval(id);
          resolve();
        }
      }, 50);
    });
  }

  // ---------------------------------------------------------------- 结束

  async function finish() {
    $("stage").classList.add("d-none");
    $("finishing").classList.remove("d-none");
    try {
      const res = await Common.api("/api/practice/finish", { session_id: sessionId, records });
      location.href = res.result_url;
    } catch (err) {
      Common.toast(`保存失败：${err.message}`, "danger");
      $("finishing").classList.add("d-none");
      $("setup").classList.remove("d-none");
      $("btn-start").disabled = false;
    }
  }

  // 音频导入面板：词库单词默认没有真人音频，可以在这里导入自己的录音。
  // 听读写不能把单词拼写画进面板（面板挂在题目下方，写了就等于泄答案），看读译不受影响。
  if (window.WordAudio && $("audio-input")) {
    bindWordAudio(() => currentWord, { revealWord: practiceType !== "listen-spell" });
    Common.getJson("/api/words/all")
      .then(data => WordAudio.setWordList(data.words))
      .catch(() => {});
  }

  setStageTitle("准备开始");
})();
