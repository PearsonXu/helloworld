const $ = (sel) => document.querySelector(sel);
const MB = 1024 * 1024;

let config = null;
// 列表中的文件：{ id, filename, size, ok, errors, warnings, status, local }
const files = [];
let localSeq = 0;

function formatSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < MB) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / MB).toFixed(1)} MB`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({ error: `请求失败（${res.status}）` }));
  if (!res.ok) throw new Error(data.error || `请求失败（${res.status}）`);
  return data;
}

// ---------- 设置 ----------

function fillForm(cfg) {
  const form = $("#config-form");
  for (const [key, value] of Object.entries(cfg)) {
    if (form.elements[key] && key !== "smtp_password") form.elements[key].value = value;
  }
  form.elements.smtp_password.value = "";
  $("#pwd-hint").textContent = cfg.has_password ? "（已保存，留空则不修改）" : "（QQ / 163 邮箱请使用授权码）";
  $("#formats").textContent = `支持：${cfg.supported_formats.map((f) => f.toUpperCase()).join(" · ")}`;

  const ready = cfg.kindle_email && cfg.sender_email && cfg.smtp_host;
  const pill = $("#settings-status");
  pill.textContent = ready ? `发送至 ${cfg.kindle_email}` : "未设置";
  pill.className = `pill ${ready ? "ok" : "err"}`;
  $("#settings").open = !ready;
}

async function loadConfig() {
  config = await api("/api/config");
  fillForm(config);
}

function showMsg(text, ok) {
  const el = $("#config-msg");
  el.textContent = text;
  el.className = `msg ${ok ? "ok" : "err"}`;
}

$("#preset").addEventListener("change", (e) => {
  if (!e.target.value) return;
  const [host, port, security, limit] = e.target.value.split("|");
  const form = $("#config-form");
  form.elements.smtp_host.value = host;
  form.elements.smtp_port.value = port;
  form.elements.smtp_security.value = security;
  form.elements.smtp_limit_mb.value = limit;
});

$("#config-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  try {
    const saved = await api("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    config = { ...config, ...saved };
    fillForm(config);
    $("#settings").open = true;
    showMsg("已保存", true);
  } catch (err) {
    showMsg(err.message, false);
  }
});

$("#test-btn").addEventListener("click", async () => {
  showMsg("正在连接…", true);
  try {
    await api("/api/config/test", { method: "POST" });
    showMsg("连接成功（请先保存最新设置再测试）", true);
  } catch (err) {
    showMsg(err.message, false);
  }
});

// ---------- 文件检查 ----------

function maxFileBytes() {
  return Math.min(config.max_file_mb, config.amazon_limit_mb) * MB;
}

// 在浏览器端先做一次快速检查，避免上传明显不合格的大文件
function precheck(file) {
  const ext = file.name.includes(".") ? file.name.split(".").pop().toLowerCase() : "";
  const errors = [];
  if (!config.supported_formats.includes(ext)) {
    errors.push(ext ? `.${ext} 格式不受 Kindle 支持` : "文件没有扩展名，无法确定格式");
  }
  if (file.size === 0) errors.push("文件为空（0 字节）");
  else if (file.size > maxFileBytes()) {
    errors.push(`文件大小 ${formatSize(file.size)} 超过上限 ${formatSize(maxFileBytes())}`);
  }
  return errors;
}

async function addFiles(fileList) {
  const toUpload = [];
  for (const file of fileList) {
    const errors = precheck(file);
    if (errors.length) {
      // 格式不支持时仍交给服务器做详细诊断（如 MOBI 转换建议），超大文件则直接拦截
      if (file.size > 0 && file.size <= maxFileBytes()) {
        toUpload.push(file);
      } else {
        files.push({ id: `local-${localSeq++}`, filename: file.name, size: file.size, ok: false, errors, warnings: [], status: "checked", local: true });
      }
    } else {
      toUpload.push(file);
    }
  }
  if (!toUpload.length) return render();

  const placeholders = toUpload.map((file) => {
    const item = { id: `pending-${localSeq++}`, filename: file.name, size: file.size, ok: false, errors: [], warnings: [], status: "checking", local: true };
    files.push(item);
    return item;
  });
  render();

  const body = new FormData();
  toUpload.forEach((f) => body.append("files", f, f.name));
  try {
    const data = await api("/api/files", { method: "POST", body });
    data.files.forEach((report, i) => Object.assign(placeholders[i], report, { status: "checked", local: false }));
  } catch (err) {
    placeholders.forEach((p) => Object.assign(p, { status: "checked", errors: [`上传失败：${err.message}`] }));
  }
  render();
}

async function removeFile(id) {
  const idx = files.findIndex((f) => f.id === id);
  if (idx < 0) return;
  const [item] = files.splice(idx, 1);
  render();
  if (!item.local) await api(`/api/files/${id}`, { method: "DELETE" }).catch(() => {});
}

function statusBadge(item) {
  switch (item.status) {
    case "checking": return `<span class="status pending">检查中…</span>`;
    case "sending": return `<span class="status pending">发送中…</span>`;
    case "sent": return `<span class="status ok">已发送</span>`;
    case "failed": return `<span class="status err">发送失败</span>`;
  }
  if (!item.ok) return `<span class="status err">不可发送</span>`;
  if (item.warnings.length) return `<span class="status warn">通过（有提醒）</span>`;
  return `<span class="status ok">检查通过</span>`;
}

function render() {
  const tbody = $("#file-table tbody");
  tbody.innerHTML = files.map((item) => {
    const issues = [
      ...item.errors.map((e) => `<li class="err">${escapeHtml(e)}</li>`),
      ...item.warnings.map((w) => `<li class="warn">${escapeHtml(w)}</li>`),
    ].join("");
    const detected = item.detected ? ` <span class="muted">· 识别为 ${escapeHtml(item.detected.toUpperCase())}</span>` : "";
    return `<tr>
      <td class="name">${escapeHtml(item.filename)}</td>
      <td class="size">${formatSize(item.size)}</td>
      <td>${statusBadge(item)}${detected}${issues ? `<ul class="issues">${issues}</ul>` : ""}</td>
      <td class="remove"><button title="移除" data-id="${escapeHtml(item.id)}" ${item.status === "sending" ? "disabled" : ""}>×</button></td>
    </tr>`;
  }).join("");

  $("#file-table").hidden = files.length === 0;
  $("#send-bar").hidden = files.length === 0;
  const sendable = files.filter(isSendable);
  const busy = files.some((f) => f.status === "checking" || f.status === "sending");
  const btn = $("#send-btn");
  btn.disabled = busy || sendable.length === 0;
  btn.textContent = sendable.length ? `发送 ${sendable.length} 本到 Kindle` : "发送到 Kindle";
}

const isSendable = (f) => !f.local && f.ok && (f.status === "checked" || f.status === "failed");

$("#file-table").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-id]");
  if (btn) removeFile(btn.dataset.id);
});

$("#clear-btn").addEventListener("click", () => {
  [...files].forEach((f) => f.status !== "sending" && removeFile(f.id));
});

$("#send-btn").addEventListener("click", async () => {
  if (!(config.kindle_email && config.sender_email && config.smtp_host)) {
    $("#settings").open = true;
    showMsg("请先完成邮箱设置", false);
    return;
  }
  const targets = files.filter(isSendable);
  targets.forEach((f) => { f.status = "sending"; });
  render();
  try {
    const data = await api("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: targets.map((f) => f.id), convert_pdf: $("#convert-pdf").checked }),
    });
    for (const r of data.results) {
      const item = files.find((f) => f.id === r.id);
      if (!item) continue;
      item.status = r.ok ? "sent" : "failed";
      if (!r.ok) item.errors = [r.error];
      if (r.ok) item.local = true; // 服务器已删除暂存文件
    }
  } catch (err) {
    targets.forEach((f) => { f.status = "failed"; f.errors = [err.message]; });
  }
  render();
});

// ---------- 拖放 ----------

const dz = $("#dropzone");
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", (e) => addFiles(e.dataTransfer.files));
$("#file-input").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });

loadConfig().catch((err) => showMsg(err.message, false));
