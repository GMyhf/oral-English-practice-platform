/* vowel.js · 近似音区分练习
   流程按设计文档：屏幕标出两个单词 1/2 → 随机播放其中一个 → 限时内按键盘 1/2 →
   立刻给出对错并进入下一题；结束后一次性提交全部作答，跳转到结果页逐题回看。 */
(() => {
  const $ = id => document.getElementById(id);

  let sessionId = null;
  let questions = [];
  let cursor = 0;
  let records = [];
  let stats = { right: 0, wrong: 0 };
  let timeLimit = 1;
  let timer = null;
  let picked = null;
  let answered = false;
  let running = false;

  function readParams() {
    return {
      group_key: $("group_key").value,
      count: Number($("count").value) || 10,
      mode: (document.querySelector('input[name="mode"]:checked') || {}).value || "word",
      voice: (document.querySelector('input[name="voice"]:checked') || {}).value || "f",
      time_limit: Number($("time_limit").value) || 1
    };
  }

  $("btn-start").addEventListener("click", async () => {
    const params = readParams();
    $("btn-start").disabled = true;
    try {
      const sess = await Common.api("/api/practice/session", {
        practice_type: "vowel", params
      });
      sessionId = sess.session_id;

      const data = await Common.api("/api/vowel/questions", params);
      questions = data.questions;
      timeLimit = params.time_limit;
      if (!questions.length) throw new Error("没有抽到题目");

      $("setup").classList.add("d-none");
      $("stage").classList.remove("d-none");
      running = true;
      document.addEventListener("keydown", onKey);
      nextQuestion();
    } catch (err) {
      Common.toast(err.message, "danger");
      $("btn-start").disabled = false;
    }
  });

  function onKey(e) {
    if (!running || answered) return;
    if (e.key === "1" || e.key === "2") {
      e.preventDefault();
      picked = Number(e.key);
      if (timer) timer.stop(true);   // 已作答，提前结束倒计时
    }
  }

  function renderOptions(question) {
    $("options").innerHTML = question.options.map(opt => `
      <div class="option-chip" data-index="${opt.index}">
        <div class="num">${opt.index}</div>
        <div class="txt">${opt.word}</div>
      </div>`).join("");
  }

  async function nextQuestion() {
    if (cursor >= questions.length) return finish();

    const question = questions[cursor];
    picked = null;
    answered = false;

    $("progress").textContent = `第 ${cursor + 1} / ${questions.length} 题`;
    $("score").textContent = `对 ${stats.right} · 错 ${stats.wrong}`;
    $("feedback").innerHTML = "";
    renderOptions(question);
    $("hint").innerHTML = '正在播放音频…请按键盘 <kbd>1</kbd> 或 <kbd>2</kbd> 作答';

    Common.playUrl(question.audio_url).catch(() => {
      $("hint").textContent = "音频播放失败，可直接按 1 / 2 猜测";
    });

    timer = Common.startCountdown(timeLimit, left => {
      const ratio = Math.max(0, Math.min(1, left / timeLimit));
      $("timer-fill").style.width = `${ratio * 100}%`;
      $("timer-fill").classList.toggle("urgent", ratio < 0.3);
      $("timer-text").textContent = `剩余 ${left.toFixed(1)} 秒`;
    });

    const timedOut = !(await timer.promise);
    answered = true;
    grade(question, timedOut ? null : picked);
  }

  function grade(question, index) {
    const correct = index !== null && index === question.answer;
    if (correct) stats.right++; else stats.wrong++;

    document.querySelectorAll(".option-chip").forEach(chip => {
      const chipIndex = Number(chip.dataset.index);
      if (chipIndex === question.answer) chip.classList.add("is-answer");
      if (chipIndex === index) chip.classList.add("picked");
    });

    if (index === null) {
      $("feedback").innerHTML =
        `<div class="result-wrong"><i class="bi bi-hourglass-split me-2"></i>无回答` +
        ` —— 正确的是 <b>${question.answer}</b>（${question.options[question.answer - 1].word}）</div>`;
    } else if (correct) {
      $("feedback").innerHTML =
        `<div class="result-correct"><i class="bi bi-check-circle-fill me-2"></i>答对了` +
        ` —— 播放的是 <b>${question.options[question.answer - 1].word}</b></div>`;
    } else {
      $("feedback").innerHTML =
        `<div class="result-wrong"><i class="bi bi-x-circle-fill me-2"></i>答错了` +
        ` —— 你按了 ${index}，播放的是 <b>${question.options[question.answer - 1].word}</b></div>`;
    }

    $("hint").textContent = "即将进入下一题…";

    records.push({
      seq: question.seq,
      type: "vowel",
      question: {
        type: "vowel",
        group_key: question.group_key,
        options: question.options,
        answer: question.answer,
        audio_url: question.audio_url
      },
      user_answer: index,
      is_correct: correct
    });

    cursor++;
    setTimeout(nextQuestion, 700);
  }

  async function finish() {
    running = false;
    document.removeEventListener("keydown", onKey);
    $("stage").classList.add("d-none");
    $("finishing").classList.remove("d-none");
    try {
      const res = await Common.api("/api/practice/finish", {
        session_id: sessionId, records
      });
      location.href = res.result_url;
    } catch (err) {
      Common.toast(`保存失败：${err.message}`, "danger");
      $("finishing").classList.add("d-none");
      $("setup").classList.remove("d-none");
      $("btn-start").disabled = false;
    }
  }
})();
