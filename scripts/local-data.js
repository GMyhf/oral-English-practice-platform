/* local-data.js · 本地模式的「我的数据」页（/account 在 APP_MODE=local 时渲染的那张）
   服务器模式下这一栏是「账号」（改用户名密码 + 最近练习），数据都在库里，页面由 Jinja 画。
   本地模式没有账号，也不该让人以为有个账号，所以这里换成数据的三个真问题：
     1) 这台设备上存了多少、还够不够存；
     2) 浏览器会不会自动清掉（持久化存储申请 + 导出备份）；
     3) 想删哪一场就删哪一场。

   所有数字都是从 IndexedDB 现读的，服务器不提供任何一个 —— 这正是本地模式的定义。 */
(() => {
  const $ = id => document.getElementById(id);
  const esc = text => String(text === null || text === undefined ? "" : text)
    .replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  if (!window.LocalStore) return;

  const TYPE_NAME = { vowel: "近似音区分", "read-translate": "看读译", "listen-spell": "听读写" };

  function humanBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1048576) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1073741824) return `${(value / 1048576).toFixed(1)} MB`;
    return `${(value / 1073741824).toFixed(2)} GB`;
  }

  function message(variant, html) {
    const box = $("io-message");
    if (!box) return;
    box.className = `alert py-2 mt-3 alert-${variant}`;
    box.innerHTML = html;
    box.classList.remove("d-none");
  }

  function setBusy(busy) {
    ["btn-export-audio", "btn-export-lite", "btn-clear"].forEach(id => {
      const button = $(id);
      if (button) button.disabled = busy;
    });
  }

  async function refresh() {
    const stats = await LocalStore.summary();
    $("sum-sessions").textContent = stats.sessions;
    $("sum-records").textContent = stats.records;
    $("sum-clips").textContent = stats.clips;
    $("sum-bytes").textContent = humanBytes(stats.bytes);

    const quota = await LocalStore.estimate();
    $("quota-line").textContent = quota
      ? `浏览器给本站的空间：已用 ${humanBytes(quota.usage)} / 约 ${humanBytes(quota.quota)} 可用` +
        "（这个上限由浏览器按磁盘空闲算，不是本站设的）"
      : "这台浏览器不报告存储配额，请以导出备份为准。";

    const persisted = await LocalStore.persistence();
    const line = $("persist-line");
    if (persisted === null) {
      line.innerHTML = '<span class="text-muted small"><i class="bi bi-question-circle me-1"></i>' +
        "这台浏览器不提供持久化存储接口，请务必定期导出备份。</span>";
    } else if (persisted) {
      line.innerHTML = '<span class="badge text-bg-success"><i class="bi bi-shield-check me-1"></i>' +
        "已获准持久化存储：浏览器不会在空间紧张时自动清掉这些数据</span>";
    } else {
      line.innerHTML = '<button class="btn btn-sm btn-outline-warning" id="btn-persist">' +
        '<i class="bi bi-shield-exclamation me-1"></i>申请持久化存储</button>' +
        '<span class="text-muted small ms-2">现在浏览器随时可以清掉这些数据，点一下申请不让它清</span>';
      const button = $("btn-persist");
      if (button) {
        button.addEventListener("click", async () => {
          button.disabled = true;
          const result = await LocalStore.requestPersistence();
          if (result.ok) {
            message("success", result.already ? "已经获准过了，无需重复申请。" :
              "浏览器已批准持久化存储，这条站的数据不会被自动驱逐了。");
          } else {
            message("warning", "浏览器没有批准这次申请（" + esc(result.reason || "可能被无痕模式或站点权限设置拦住") +
              "）。这种情况下数据确实可能被清掉，<strong>请养成定期导出备份的习惯</strong>。");
          }
          await refresh();
        });
      }
    }

    const last = await LocalStore.lastBackup();
    $("backup-line").textContent = last
      ? `上次导出：${last.at} · ${last.filename}（${humanBytes(last.size)}，` +
        `${last.withAudio ? "含录音" : "只有记录"}）`
      : "还没有导出过备份。";

    await renderSessions();
  }

  async function renderSessions() {
    const sessions = await LocalStore.listSessions(200);
    const counts = await LocalStore.clipCountBySession();
    const rows = $("session-rows");
    if (!sessions.length) {
      rows.innerHTML = "";
      $("no-sessions").classList.remove("d-none");
      return;
    }
    $("no-sessions").classList.add("d-none");
    rows.innerHTML = sessions.map(item => {
      const unfinished = !item.finished;
      return `
      <tr class="${unfinished ? "table-light" : ""}">
        <td class="small text-muted">${esc(item.created_at)}${unfinished ? "（这场没记完）" : ""}</td>
        <td>${esc(TYPE_NAME[item.practice_type] || item.practice_type)}</td>
        <td class="text-end">${esc(item.total)}</td>
        <td class="text-end text-success fw-semibold">${esc(item.correct_count)}</td>
        <td class="text-end text-danger fw-semibold">${esc(item.wrong_count)}</td>
        <td class="text-end">${counts[item.id] || 0}</td>
        <td class="text-end text-nowrap">
          <a class="btn btn-sm btn-outline-secondary" href="/result/${esc(item.id)}">回看</a>
          <button class="btn btn-sm btn-outline-danger" data-del="${esc(item.id)}">删除</button>
        </td>
      </tr>`;
    }).join("");

    rows.querySelectorAll("[data-del]").forEach(button => {
      button.addEventListener("click", async () => {
        const id = Number(button.dataset.del);
        const clips = counts[id] || 0;
        if (!confirm(`删除会话 #${id}？${clips ? `连同它的 ${clips} 条录音一起删，` : ""}本地模式删掉就没有第二份了。`)) return;
        try {
          await LocalStore.deleteSession(id);
          message("success", `已删除会话 #${id}。`);
          await refresh();
        } catch (err) {
          message("danger", `删除失败：${esc(err.message)}`);
        }
      });
    });
  }

  function saveText(text, filename) {
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    // 立刻回收会让某些浏览器（尤其 iOS Safari）下载中断，留一分钟
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    return blob.size;
  }

  async function exportBackup(includeAudio) {
    setBusy(true);
    message("secondary", "正在打包本机的练习记录…");
    try {
      const result = await LocalStore.exportBackup({ includeAudio });
      const bytes = saveText(result.text, result.filename);
      await LocalStore.markBackup(result.filename, result.withAudio, bytes);
      const detail = result.skippedAudio
        ? `<div class="small text-muted mt-1">${esc(result.reason)}</div>` : "";
      const counts = result.counts || {};
      // 一场练习可能根本没有录音（麦克风被拒），那时这句里不该出现「0 条录音，不含录音」；
      // 有录音时必须说清在不在文件里 —— 这是用户判断「这份备份够不够」的唯一依据。
      const clipPart = (counts.audio_clips || 0)
        ? `、${counts.audio_clips} 条录音${result.withAudio ? "已含在文件里" : "，不含录音"}`
        : "";
      message("success", `已导出 <strong>${esc(result.filename)}</strong>（${humanBytes(bytes)}，` +
        `${counts.sessions || 0} 场 / ${counts.records || 0} 条作答${clipPart}）。` +
        `请把这个文件存到网盘或 U 盘里 —— 它一丢，这台设备上的记录就没有第二份了。${detail}`);
      await refresh();
    } catch (err) {
      message("danger", `导出失败：${esc(err.message)}`);
    } finally {
      setBusy(false);
    }
  }

  async function importBackup(file) {
    message("secondary", "正在读取备份文件…");
    const text = file.text ? await file.text() : await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(reader.error || new Error("文件读取失败"));
      reader.readAsText(file);
    });
    setBusy(true);
    try {
      const result = await LocalStore.importBackup(text);
      message("success", `已导入 ${result.sessions} 场练习、${result.records} 条作答、` +
        `${result.audio_clips} 条录音。${esc(result.hint || "")}` +
        "<div class=\"small text-muted mt-1\">导入是追加：这些场次拿到了新的本机编号，原有的记录没有被改动。</div>");
      await refresh();
    } catch (err) {
      message("danger", `导入失败：${esc(err.message)}`);
    } finally {
      setBusy(false);
    }
  }

  $("btn-export-audio").addEventListener("click", () => exportBackup(true));
  $("btn-export-lite").addEventListener("click", () => exportBackup(false));

  $("import-file").addEventListener("change", async event => {
    const file = event.target.files[0];
    event.target.value = "";
    if (file) await importBackup(file);
  });

  $("btn-clear").addEventListener("click", async () => {
    const stats = await LocalStore.summary();
    if (!stats.sessions && !stats.clips) {
      message("secondary", "本机本来就是空的，没有需要清的东西。");
      return;
    }
    if (!confirm(`确定清空这台设备上的全部练习记录？\n` +
      `${stats.sessions} 场练习、${stats.records} 条作答、${stats.clips} 条录音都会被删掉，` +
      `本地模式没有服务器副本可以恢复。设置（时长、题量）不会被清。\n\n建议先点「导出（含录音）」留一份。`)) return;
    setBusy(true);
    try {
      await LocalStore.clearAll();
      message("success", "已清空本机全部练习记录与录音。");
      await refresh();
    } catch (err) {
      message("danger", `清空失败：${esc(err.message)}`);
    } finally {
      setBusy(false);
    }
  });

  refresh().catch(err => message("danger", `读不出本机数据：${esc(err.message)}`));
})();
