/* 肥鱼娘 App 前端逻辑：REST + SSE */
"use strict";

const $ = (s, el) => (el || document).querySelector(s);
const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
const SESSION = "web_" + Math.random().toString(36).slice(2, 8);

const state = {
  status: null,
  chatBusy: false,
  memTab: "profiles",
  memUser: "",
  memQ: "",
  plugins: null,
  config: null,
  botId: "feiyu",
  defaultBot: "feiyu",
  bots: [],
};

/* ---------------- 基础请求 ---------------- */
async function api(path, opts) {
  const r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts));
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
const GET = (p) => api(p);
const POST = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });

/* 带 bot_id 的请求包装：切换 Bot 后，所有相关接口都带上当前 bot_id */
function withBot(p, params) {
  const u = new URL(p, location.origin);
  const qs = new URLSearchParams(u.search);
  if (params) Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null) qs.set(k, v); });
  qs.set("bot_id", state.botId);
  u.search = qs.toString();
  return u.pathname + u.search;
}
const GB = (p, params) => GET(withBot(p, params));
const PB = (p, body) => POST(p, Object.assign({ bot_id: state.botId }, body || {}));

/* ---------------- 本地 AI 依赖按需后装 ---------------- */
function initLocalDeps() {
  const fill = $("#localDepsFill"), bar = fill ? fill.parentElement : null;
  const logBox = $("#localDepsLog"), msg = $("#localDepsMsg");
  const btnCpu = $("#btnInstallLocalCpu"), btnCuda = $("#btnInstallLocalCuda");
  let timer = null, lastLog = 0;

  async function poll() {
    try {
      const s = await GET("/api/system/install_local_deps");
      fill.style.width = (s.percent || 0) + "%";
      if (s.running) bar.classList.add("show");
      if (s.log && s.log.length > lastLog) {
        const add = s.log.slice(lastLog); lastLog = s.log.length;
        logBox.classList.add("show");
        for (const l of add) { const d = document.createElement("div"); d.textContent = l; logBox.appendChild(d); }
        logBox.scrollTop = logBox.scrollHeight;
      }
      if (s.done) { msg.textContent = "安装完成，本地能力已可用 ✅"; msg.style.color = "var(--ok)"; clearInterval(timer); timer = null; btnCpu.disabled = false; btnCuda.disabled = false; }
      if (s.error) { msg.textContent = "安装失败：" + s.error; msg.style.color = "var(--err)"; clearInterval(timer); timer = null; btnCpu.disabled = false; btnCuda.disabled = false; }
    } catch (e) { /* 后端未就绪，忽略 */ }
  }

  async function install(mode) {
    if (timer) return;
    btnCpu.disabled = true; btnCuda.disabled = true;
    msg.textContent = "正在后台安装，请稍候…"; msg.style.color = "var(--muted)";
    logBox.innerHTML = ""; lastLog = 0; bar.classList.add("show"); logBox.classList.add("show");
    try {
      const r = await POST("/api/system/install_local_deps", { mode });
      if (!r.ok) { msg.textContent = "已在安装中或启动失败"; btnCpu.disabled = false; btnCuda.disabled = false; return; }
    } catch (e) { msg.textContent = e.message || "请求失败"; btnCpu.disabled = false; btnCuda.disabled = false; return; }
    timer = setInterval(poll, 800);
  }

  if (btnCpu) btnCpu.addEventListener("click", () => install("cpu"));
  if (btnCuda) btnCuda.addEventListener("click", () => install("cuda"));
  // 进入配置页时若正在安装则恢复轮询
  poll();
}

let toastTimer = null;
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("err", !!isErr);
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 2600);
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (ts) => { try { return new Date(ts * 1000).toLocaleTimeString(); } catch (e) { return ""; } };

/* ---------------- 页面切换 ---------------- */
$$(".nav-item").forEach(el => el.addEventListener("click", () => {
  $$(".nav-item").forEach(x => x.classList.toggle("active", x === el));
  $$(".page").forEach(p => p.classList.add("hidden"));
  $("#page-" + el.dataset.page).classList.remove("hidden");
  const loaders = { dashboard: loadDashboard, chat: null, memory: loadMemory, summary: loadSummary, plugins: loadPlugins, config: loadConfig, issues: loadIssuesPage, appearance: loadAppearance, builder: loadBuilder };
  const fn = loaders[el.dataset.page];
  if (fn) fn().catch(e => toast(e.message, true));
}));

/* ---------------- SSE ---------------- */
function connectSSE() {
  const es = new EventSource("/api/events?session=" + SESSION);
  es.onmessage = (m) => {
    if (!m.data || m.data.startsWith(":")) return;
    try { handleEvent(JSON.parse(m.data)); } catch (e) { }
  };
  es.onerror = () => { es.close(); setTimeout(connectSSE, 3000); };
}

/* 事件去重：SSE 回放/重连/初始拉取可能重叠，按 id（或指纹）只渲染一次 */
const seenIds = new Set();
function addEventOnce(ev) {
  const key = ev.id != null ? "id:" + ev.id
    : "fp:" + ev.type + "|" + (ev.text || ev.path || "") + "|" + Math.round((ev.ts || 0) * 1000);
  if (seenIds.has(key)) return false;
  seenIds.add(key);
  return true;
}

function handleEvent(ev) {
  if (!addEventOnce(ev)) return;
  appendEventLog(ev);
  if (ev.type === "message") {
    if (ev.role === "assistant" && ev.text) addChatMsg("assistant", ev.text);
    else if (ev.role === "user" && ev.text) addChatMsg("user", ev.text, true);
  } else if (ev.type === "image" && ev.path) {
    addChatImage(ev.path);
  } else if (ev.type === "audio" && ev.path) {
    addChatAudio(ev.path);
  } else if (ev.type === "status") {
    state.chatBusy = ev.state === "thinking";
    const chip = $("#chatState");
    chip.textContent = ev.state === "thinking" ? "思考中…" : "空闲";
    chip.classList.toggle("busy", state.chatBusy);
  } else if (ev.type === "error") {
    addChatMsg("error", "出错了: " + ev.text);
  }
  refreshCorePill();
}

function addChatImage(path) {
  const log = $("#chatLog");
  const div = document.createElement("div");
  div.className = "msg assistant";
  const img = document.createElement("img");
  img.className = "chat-img";
  img.src = "/api/file?path=" + encodeURIComponent(path);
  img.onclick = () => window.open(img.src, "_blank");
  div.appendChild(img);
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function addChatAudio(path) {
  const log = $("#chatLog");
  const div = document.createElement("div");
  div.className = "msg assistant";
  const au = document.createElement("audio");
  au.controls = true;
  au.src = "/api/file?path=" + encodeURIComponent(path);
  au.play().catch(() => { });
  div.appendChild(au);
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function appendEventLog(ev) {
  const log = $("#eventLog");
  const div = document.createElement("div");
  div.className = "ev";
  const text = ev.type === "message" ? `[${ev.role}] ${(ev.text || "").slice(0, 80)}`
    : ev.type === "status" ? `状态: ${ev.state}` : `${ev.type}: ${(ev.text || JSON.stringify(ev)).slice(0, 90)}`;
  div.innerHTML = `<span class="t">${fmtTime(ev.ts)}</span>${esc(text)}`;
  log.prepend(div);
  while (log.children.length > 80) log.lastChild.remove();
}

/* ---------------- 仪表盘 ---------------- */
async function loadDashboard() {
  await refreshStatus();
  renderBotCard();
  try {
    const ms = await GB("/api/memory/stats");
    $("#stProfiles").textContent = ms.profiles;
    $("#stNotes").textContent = ms.notes;
    $("#stKnowledge").textContent = ms.knowledge;
    $("#stReflection").textContent = ms.reflection;
  } catch (e) { }
}

function renderBotCard() {
  const b = state.bots.find(x => x.id === state.botId) || { id: state.defaultBot, name: "肥鱼娘", persona: null, running: false, autostart: true, model: null };
  const el = $("#botCard");
  if (!el) return;
  el.innerHTML = `<div class="panel-title">当前查看的 Bot：${esc(b.name || b.id)} <span class="badge ${b.running ? "ok" : "off"}">${b.running ? "运行中" : "已停止"}</span></div>
    <div class="hint wrap" style="margin:6px 0">${esc(b.persona || "（沿用基座人设）")}</div>
    <div class="row-actions">
      <button class="btn ghost sm botManageBtn" type="button">管理 Bot</button>
      <span class="hint">模型覆盖：${esc(b.model || "全局默认")} · 自启：${b.autostart ? "是" : "否"}</span>
    </div>`;
}

async function refreshStatus() {
  try {
    const st = await GET("/api/status");
    state.status = st;
    $("#stCore").textContent = st.core ? "运行中" : (st.starting ? "启动中" : "已停止");
    $("#stPlugins").textContent = `${st.counts.plugins_running}/${st.counts.plugins_total}`;
    renderStatusTable(st);
  } catch (e) {
    $("#stCore").textContent = "离线";
  }
  refreshCorePill();
}

function renderStatusTable(st) {
  const rows = [];
  (st.plugins || []).forEach(p => rows.push([p.name, "插件", p.running ? "运行中" : "已停", p.status_detail || ""]));
  (st.brains || []).forEach(b => rows.push([b.name, "大脑", b.running ? "运行中" : "未运行", b.description || b.title || ""]));
  const tb = $("#tblStatus tbody");
  tb.innerHTML = rows.map(r => `<tr><td>${esc(r[0])}</td><td><span class="badge">${r[1]}</span></td><td>${esc(r[2])}</td><td class="wrap">${esc(r[3])}</td></tr>`).join("") || `<tr><td colspan="4" class="hint">核心未启动</td></tr>`;
}

function refreshCorePill() {
  const pill = $("#corePill");
  const on = !!(state.status && state.status.core);
  pill.classList.toggle("on", on);
  pill.classList.toggle("off", !on);
  $("#corePillText").textContent = on ? "核心运行中" : "核心未启动";
  $("#btnCoreToggle").textContent = on ? "停止核心" : "启动核心";
}

$("#btnCoreToggle").addEventListener("click", async () => {
  const on = !!(state.status && state.status.core);
  const btn = $("#btnCoreToggle");
  btn.disabled = true; btn.textContent = on ? "停止中…" : "启动中…";
  try {
    const r = await POST(on ? "/api/core/stop" : "/api/core/start");
    if (!r.ok) throw new Error(r.error || "失败");
    toast(on ? "核心已停止" : "核心已启动");
    await refreshStatus();
  } catch (e) { toast(e.message, true); }
  btn.disabled = false;
  refreshCorePill();
});

/* ---------------- 聊天 ---------------- */
function addChatMsg(role, text, local) {
  const log = $("#chatLog");
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.textContent = text;
  if (role !== "user" || !local) {
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = fmtTime(Date.now() / 1000);
    div.appendChild(meta);
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : s;
  return d.innerHTML;
}

function addChatMsgHtml(role, html) {
  const log = $("#chatLog");
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.innerHTML = html;
  if (role !== "user") {
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = fmtTime(Date.now() / 1000);
    div.appendChild(meta);
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function sendChat() {
  const input = $("#chatInput");
  const text = input.value.trim();
  const hasMedia = pendingImages.length || pendingVideo || pendingAudio;
  if (!text && !hasMedia) return;
  if (state.chatBusy) { toast("她还在思考中，稍等一下…"); return; }
  input.value = "";
  // 乐观锁：立即置忙，不等 SSE status 回包（防快速双击导致重复提交/重复回复）
  state.chatBusy = true;
  const chip = $("#chatState");
  chip.textContent = "思考中…"; chip.classList.add("busy");
  // 本地乐观渲染用户消息（含附件缩略）
  let userHtml = "";
  if (text) userHtml += escapeHtml(text);
  pendingImages.forEach(it => { userHtml += `<br><img class="chat-att" src="${it.dataUrl}">`; });
  if (pendingVideo) userHtml += `<br><span class="chat-att-tag">🎬 视频</span>`;
  if (pendingAudio) userHtml += `<br><span class="chat-att-tag">🎤 语音</span>`;
  addChatMsgHtml("user", userHtml);
  // 组装并清空待发附件
  const payload = {
    text, session: SESSION,
    images: pendingImages.map(it => it.dataUrl),
    video: pendingVideo ? pendingVideo.dataUrl : null,
    audio: pendingAudio ? pendingAudio.dataUrl : null,
  };
  pendingImages = []; pendingVideo = null; pendingAudio = null; renderPending();
  try {
    const r = await POST("/api/chat", payload);
    if (!r.ok) {
      addChatMsg("error", r.error || "发送失败");
      state.chatBusy = false;
      chip.textContent = "空闲"; chip.classList.remove("busy");
    }
  } catch (e) {
    addChatMsg("error", e.message);
    state.chatBusy = false;
    chip.textContent = "空闲"; chip.classList.remove("busy");
  }
}
$("#btnSend").addEventListener("click", sendChat);
$("#chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
$("#btnClearChat").addEventListener("click", () => { $("#chatLog").innerHTML = ""; });

// ---------------- 聊天附件：图片 / 视频 / 语音 ----------------
let pendingImages = [];   // [{dataUrl, mime, name}]
let pendingVideo = null; // {dataUrl, mime, name} | null
let pendingAudio = null; // {dataUrl, mime} | null
let mediaRecorder = null, recChunks = [];

function fileToDataURL(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}
function renderPending() {
  const box = $("#chatPreview");
  if (!box) return;
  box.innerHTML = "";
  const add = (label, thumb, remove) => {
    const d = document.createElement("div");
    d.className = "prev-item";
    if (thumb) { const i = document.createElement("img"); i.src = thumb; d.appendChild(i); }
    else { const s = document.createElement("span"); s.textContent = label; d.appendChild(s); }
    const x = document.createElement("button");
    x.className = "prev-x"; x.type = "button"; x.textContent = "×";
    x.onclick = remove; d.appendChild(x);
    box.appendChild(d);
  };
  pendingImages.forEach((it, i) => add("图片", it.dataUrl, () => { pendingImages.splice(i, 1); renderPending(); }));
  if (pendingVideo) add("视频", null, () => { pendingVideo = null; renderPending(); });
  if (pendingAudio) add("语音", null, () => { pendingAudio = null; renderPending(); });
}
$("#btnImg").addEventListener("click", () => $("#fileImg").click());
$("#fileImg").addEventListener("change", async (e) => {
  for (const f of e.target.files) {
    pendingImages.push({ dataUrl: await fileToDataURL(f), mime: f.type, name: f.name });
  }
  e.target.value = ""; renderPending();
});
$("#btnVideo").addEventListener("click", () => $("#fileVideo").click());
$("#fileVideo").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (f) pendingVideo = { dataUrl: await fileToDataURL(f), mime: f.type, name: f.name };
  e.target.value = ""; renderPending();
});
$("#btnVoice").addEventListener("click", async () => {
  if (mediaRecorder && mediaRecorder.state !== "inactive") { mediaRecorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    mediaRecorder = new MediaRecorder(stream);
    recChunks = [];
    mediaRecorder.ondataavailable = (ev) => { if (ev.data && ev.data.size) recChunks.push(ev.data); };
    mediaRecorder.onstop = async () => {
      const blob = new Blob(recChunks, { type: mediaRecorder.mimeType || "audio/webm" });
      pendingAudio = { dataUrl: await fileToDataURL(blob), mime: blob.type || "audio/webm" };
      renderPending();
      stream.getTracks().forEach(t => t.stop());
      const b = $("#btnVoice"); b.classList.remove("rec"); b.textContent = "🎤";
    };
    mediaRecorder.start();
    const b = $("#btnVoice"); b.classList.add("rec"); b.textContent = "⏹";
  } catch (err) {
    toast("无法访问麦克风：" + (err && err.message ? err.message : err));
  }
});

/* ---------------- 记忆中心 ---------------- */
$$(".tab").forEach(t => t.addEventListener("click", () => {
  $$(".tab").forEach(x => x.classList.toggle("active", x === t));
  state.memTab = t.dataset.tab;
  renderMemory().catch(e => toast(e.message, true));
}));
$("#memUser").addEventListener("change", () => { state.memUser = $("#memUser").value; renderMemory(); });
$("#memSearch").addEventListener("keydown", e => {
  if (e.key === "Enter") { state.memQ = $("#memSearch").value.trim(); renderMemory(); }
});

async function loadMemory() {
  const users = await GB("/api/memory/users");
  const sel = $("#memUser"), sumSel = $("#sumUser");
  const cur = sel.value, cur2 = sumSel.value;
  sel.innerHTML = `<option value="">全部用户</option>` + users.items.map(u =>
    `<option value="${esc(u.id)}">${esc(u.id)}${u.name ? "（" + esc(u.name) + "）" : ""}</option>`).join("");
  sumSel.innerHTML = users.items.map(u =>
    `<option value="${esc(u.id)}">${esc(u.id)}${u.name ? "（" + esc(u.name) + "）" : ""}</option>`).join("");
  sel.value = cur; sumSel.value = cur2 || "app_owner";
  state.memUser = sel.value;
  await renderMemory();
}

async function renderMemory() {
  const body = $("#memBody");
  body.innerHTML = "加载中…";
  const tab = state.memTab, uid = state.memUser, q = state.memQ;
  const qs = new URLSearchParams(); if (uid) qs.set("uid", uid); if (q) qs.set("q", q);
  const map = {
    profiles: "/api/memory/profiles", notes: "/api/memory/notes",
    persona: "/api/memory/persona", reflection: "/api/memory/reflection",
    knowledge: "/api/memory/knowledge", history: "/api/memory/history",
  };
  try {
    if (tab === "sessions") { const d = await GB("/api/memories"); body.innerHTML = renderSessions(d); return; }
    const d = await GB(map[tab], { uid, q });
    body.innerHTML = (renderers[tab] || (() => "<div class='hint'>未知页签</div>"))(d);
    bindMemoryActions();
  } catch (e) { body.innerHTML = `<div class="hint">加载失败: ${esc(e.message)}</div>`; }
}

const renderers = {
  profiles: (d) => Object.entries(d.items).map(([u, p]) => `
    <div class="mem-card" data-kind="profiles" data-uid="${esc(u)}">
      <div class="mem-head"><span class="mem-title">${esc(u)}</span>
        <span><button class="btn small primary act-save">保存</button>
        <button class="btn small danger act-del">删除档案</button></span></div>
      <textarea class="textarea facts" rows="5">${esc((p.facts || []).join("\n"))}</textarea>
      <div class="hint" style="margin-top:6px">每行一条事实，共 ${ (p.facts || []).length } 条</div>
    </div>`).join("") || "<div class='hint'>暂无人物档案</div>",
  notes: (d) => Object.entries(d.items).map(([u, list]) => `
    <div class="mem-card">
      <div class="mem-head"><span class="mem-title">${esc(u)}</span><span class="badge">${list.length} 条</span></div>
      <ul>${list.map((n, i) => `<li data-uid="${esc(u)}" data-idx="${i + 1}">
        <span class="wrap">${esc(n.text)}</span>
        <span class="hint">${esc(n.time || "")}${n.category ? " · " + esc(n.category) : ""}</span>
        <button class="btn small danger act-note-del">删除</button></li>`).join("")}</ul>
      <div class="row-actions"><input class="input new-note" placeholder="新增重要事项…" style="flex:1">
      <button class="btn small primary act-note-add">添加</button></div>
    </div>`).join("") || "<div class='hint'>暂无重要事项</div>",
  persona: (d) => Object.entries(d.items).map(([u, p]) => `
    <div class="mem-card">
      <div class="mem-head"><span class="mem-title">${esc(u)}</span>
        <button class="btn small danger" data-act="clear-persona" data-uid="${esc(u)}">清空</button></div>
      <div class="hint">风格：${esc(p.style || "（无）")}</div>
      <ul>${(p.traits || []).map(t => `<li>${esc(t)}</li>`).join("") || "<li class='hint'>暂无相处特征</li>"}</ul>
    </div>`).join("") + (d.global_style ? `<div class="mem-card"><div class="mem-title">全局风格</div><div class="wrap">${esc(d.global_style)}</div></div>` : "")
    || "<div class='hint'>暂无人格记忆</div>",
  reflection: (d) => `
    <div class="mem-card"><div class="mem-head"><span class="mem-title">统计</span></div>
      <div class="hint">对话轮数 ${d.stats.total_conversations || 0} · 反思 ${d.stats.total_reflections || 0} 次</div></div>
    ${(d.items || []).map(r => `<div class="mem-card">
      <div class="mem-head"><span class="mem-title">${esc(r.type || "?")}<span class="badge">${esc(r.user_id || "")}</span></span>
      <span class="hint">置信度 ${(r.confidence || 0).toFixed ? r.confidence.toFixed(2) : r.confidence}</span></div>
      <div class="wrap">${esc(r.content)}</div>
      ${r.evidence ? `<div class="hint" style="margin-top:4px">依据: ${esc(r.evidence)}</div>` : ""}</div>`).join("")}
    ${(d.rules || []).length ? `<div class="mem-card"><div class="mem-title">交互规则</div><ul>${d.rules.map(r => `<li>${esc(r.rule)} <span class="badge">${((r.confidence || 0) * 100 | 0)}%</span></li>`).join("")}</ul></div>` : ""}
    ${(d.items || []).length ? "" : "<div class='hint'>暂无反思记录</div>"}`,
  knowledge: (d) => (d.error ? `<div class="hint">${esc(d.error)}</div>` : "") +
    `<div class="mem-card"><div class="mem-head"><span class="mem-title">共 ${d.count} 个主题</span></div>
     <div class="row-actions"><input class="input new-kw" placeholder="主题/关键词…" style="flex:1"><input class="input new-facts" placeholder="事实（分号分隔）" style="flex:2">
     <button class="btn small primary act-kb-add">添加</button></div></div>` +
    (d.items || []).map(e => `<div class="mem-card">
      <div class="mem-head"><span class="mem-title">${esc(e.topic)}</span>
      <button class="btn small danger" data-act="kb-del" data-kw="${esc(e.topic)}">删除</button></div>
      <ul>${(e.facts || []).map(f => `<li>${esc(f)}</li>`).join("")}</ul>
      <div class="hint">${(e.keywords || []).map(esc).join(" · ")}</div></div>`).join(""),
  history: (d) => d.hint ? `<div class="hint">${esc(d.hint)}</div>` :
    `<div class="mem-card"><ul>${(d.items || []).map(h => `<li>
      <span class="badge">${esc(h.role || "?")}</span> <span class="hint">${esc(h.time || "")}</span>
      <div class="wrap">${esc(h.content || h.text || "")}</div></li>`).join("") || "<li class='hint'>暂无历史</li>"}</ul></div>`,
};

function renderSessions(d) {
  return d.items.map(s => `<div class="mem-card">
    <div class="mem-head"><span class="mem-title">${esc(s.key)}</span><span class="badge">${s.turns} 条短期记忆</span></div>
    ${s.topic ? `<div class="hint">话题: ${esc(s.topic)}</div>` : ""}
    ${s.summary ? `<div class="wrap" style="margin-top:6px">${esc(s.summary)}</div>` : ""}
    ${(s.recent || []).map(t => `<li style="list-style:none;padding:4px 0"><span class="badge">${esc(t.role || "?")}</span> ${esc((t.content || "").slice(0, 120))}</li>`).join("")}
  </div>`).join("") || "<div class='hint'>暂无会话记忆</div>";
}

function bindMemoryActions() {
  $$("#memBody .act-save").forEach(b => b.addEventListener("click", async () => {
    const card = b.closest(".mem-card");
    const uid = card.dataset.uid;
    const facts = $(".facts", card).value.split("\n").map(s => s.trim()).filter(Boolean);
    const r = await PB("/api/memory/action", { kind: "profiles", payload: { op: "save", uid, facts } });
    toast(r.ok ? "档案已保存" : (r.error || "保存失败"), !r.ok);
  }));
  $$("#memBody .act-del").forEach(b => b.addEventListener("click", async () => {
    const uid = b.closest(".mem-card").dataset.uid;
    if (!confirm(`确定删除 ${uid} 的人物档案？`)) return;
    await PB("/api/memory/action", { kind: "profiles", payload: { op: "delete", uid } });
    toast("已删除"); renderMemory();
  }));
  $$("#memBody .act-note-del").forEach(b => b.addEventListener("click", async () => {
    const li = b.closest("li");
    await PB("/api/memory/action", { kind: "notes", payload: { op: "delete", uid: li.dataset.uid, index: +li.dataset.idx } });
    renderMemory();
  }));
  $$("#memBody .act-note-add").forEach(b => b.addEventListener("click", async () => {
    const card = b.closest(".mem-card");
    const inp = $(".new-note", card);
    if (!inp.value.trim()) return;
    await PB("/api/memory/action", { kind: "notes", payload: { op: "add", uid: state.memUser || "app_owner", text: inp.value.trim() } });
    toast("已添加"); renderMemory();
  }));
  $$("#memBody [data-act='clear-persona']").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("清空该用户的人格记忆？")) return;
    await PB("/api/memory/action", { kind: "persona", payload: { op: "clear", uid: b.dataset.uid } });
    toast("已清空"); renderMemory();
  }));
  $$("#memBody [data-act='kb-del']").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("删除该知识主题？")) return;
    await PB("/api/memory/action", { kind: "knowledge", payload: { op: "delete", keyword: b.dataset.kw } });
    toast("已删除"); renderMemory();
  }));
  const kbAdd = $("#memBody .act-kb-add");
  if (kbAdd) kbAdd.addEventListener("click", async () => {
    const topic = $("#memBody .new-kw").value.trim();
    const facts = $("#memBody .new-facts").value.split(/[;；]/).map(s => s.trim()).filter(Boolean);
    if (!topic || !facts.length) return toast("主题与事实必填", true);
    const r = await PB("/api/memory/action", { kind: "knowledge", payload: { op: "add", topic, facts } });
    toast(r.ok ? "已入库" : (r.error || "失败"), !r.ok); renderMemory();
  });
}

/* ---------------- 总结中心 ---------------- */
async function loadSummary() {
  const ov = await GB("/api/summary/overview");
  const live = ov.live || {};
  $("#liveSummary").value = live.summary || "";
  $("#liveSummaryMeta").textContent = live.topic ? `当前话题: ${live.topic} · 短期记忆 ${live.short_turns} 条` : "";
  const tb = $("#tblSummaries tbody");
  tb.innerHTML = ov.items.map(i => `<tr><td>${esc(i.key)}</td><td>${i.turns}</td><td class="wrap">${esc(i.topic || "—")}</td><td class="wrap">${esc(i.summary || "—")}</td></tr>`).join("") || `<tr><td colspan="4" class="hint">暂无</td></tr>`;
}

$("#btnSaveSummary").addEventListener("click", async () => {
  const r = await PB("/api/summary/session", { text: $("#liveSummary").value });
  toast(r.ok ? (r.mode === "live" ? "已保存（运行中，立即生效）" : "已保存（落盘，下次启动核心生效）") : "保存失败", !r.ok);
});

$("#btnRunSummary").addEventListener("click", async () => {
  const btn = $("#btnRunSummary");
  btn.disabled = true; btn.textContent = "总结中…（LLM 调用约 10-30s）";
  const box = $("#sumResult"); box.classList.add("hidden");
  try {
    const r = await PB("/api/summary/run", {
      uid: $("#sumUser").value || "app_owner",
      count: +$("#sumCount").value || 60,
      save_to: $("#sumSaveTo").value,
      keyword: $("#sumKeyword").value.trim(),
    });
    if (!r.ok) throw new Error(r.error || "失败");
    box.textContent = `已读取 ${r.count} 条记录${r.saved.length ? "，沉淀到: " + r.saved.join("、") : ""}\n\n` + r.summary;
    box.classList.remove("hidden");
  } catch (e) { toast(e.message, true); }
  btn.disabled = false; btn.textContent = "生成总结";
});

/* ---------------- 多 Bot 切换与管理 ---------------- */
const DEFAULT_BOT_ID = "feiyu";

async function loadBots() {
  try {
    const r = await GET("/api/bots");
    state.bots = (r && r.bots) || [];
    state.defaultBot = (r && r.default) || DEFAULT_BOT_ID;
  } catch (e) { state.bots = []; }
  if (!state.bots.find(b => b.id === state.botId)) state.botId = state.defaultBot;
  renderBotSwitchers();
}

function renderBotSwitchers() {
  const opts = state.bots.map(b =>
    `<option value="${esc(b.id)}">${esc(b.name || b.id)}${b.id === state.defaultBot ? "（主）" : ""}</option>`).join("");
  document.querySelectorAll(".botSwitcher").forEach(sel => { sel.innerHTML = opts; sel.value = state.botId; });
  const page = document.querySelector(".page:not(.hidden)");
  if (page && page.id === "page-dashboard") renderBotCard();
  if (page && page.id === "page-config") renderBotConfigPanel();
}

function onBotSwitched() {
  const page = document.querySelector(".page:not(.hidden)");
  const id = page && page.id;
  if (id === "page-dashboard") loadDashboard();
  else if (id === "page-memory") loadMemory();
  else if (id === "page-summary") loadSummary();
  else if (id === "page-config") loadConfig();
}

document.addEventListener("change", e => {
  const sel = e.target.closest && e.target.closest(".botSwitcher");
  if (sel) { state.botId = sel.value; onBotSwitched(); }
});
document.addEventListener("click", e => {
  if (e.target.closest && e.target.closest(".botManageBtn")) { openBotModal(); return; }
  const t = e.target.closest && e.target.closest(".botStartStop");
  if (t) { botToggle(t.getAttribute("data-id")); }
});

function botModalHtml() {
  const rows = state.bots.map(b => `
    <tr>
      <td>${esc(b.name || b.id)}<div class="hint">${esc(b.id)}</div></td>
      <td class="wrap" style="max-width:260px">${esc(b.persona || "（沿用基座人设）")}</td>
      <td><span class="badge ${b.running ? "ok" : "off"}">${b.running ? "运行中" : "已停止"}</span></td>
      <td>${b.autostart ? "是" : "否"}</td>
      <td class="row-actions">
        <button class="btn small ${b.running ? "danger" : "primary"} botStartStop" data-id="${esc(b.id)}">${b.running ? "停止" : "启动"}</button>
        <button class="btn small ghost botEdit" data-id="${esc(b.id)}">编辑</button>
        <button class="btn small danger botDel" data-id="${esc(b.id)}">删除</button>
      </td>
    </tr>`).join("");
  return `
    <div class="modal-card">
      <div class="modal-head"><h3>管理 Bot</h3><button class="btn ghost" id="botModalClose">✕</button></div>
      <div class="modal-body">
        <table class="tbl"><thead><tr><th>名称</th><th>人格</th><th>状态</th><th>自启</th><th>操作</th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="hint">仅有默认 Bot（肥鱼娘）</td></tr>`}</tbody></table>
        <hr style="margin:16px 0">
        <div id="botEditForm"></div>
      </div>
    </div>`;
}

function openBotModal() {
  const modal = $("#botModal");
  modal.innerHTML = botModalHtml();
  modal.classList.remove("hidden");
  bindBotModal();
  botEditForm(null);
}

function bindBotModal() {
  $("#botModalClose").onclick = () => $("#botModal").classList.add("hidden");
  $$("#botModal .botStartStop").forEach(b => b.onclick = () => botToggle(b.getAttribute("data-id")));
  $$("#botModal .botEdit").forEach(b => b.onclick = () => botEditForm(b.getAttribute("data-id")));
  $$("#botModal .botDel").forEach(b => b.onclick = () => botDelete(b.getAttribute("data-id")));
}

function botEditForm(id) {
  const b = id ? state.bots.find(x => x.id === id) : null;
  const f = $("#botEditForm");
  if (!f) return;
  f.innerHTML = `
    <div class="panel-title">${b ? "编辑 Bot：" + esc(b.id) : "新建 Bot"}</div>
    <div class="cfg-grid">
      <label class="cfg-field" style="flex:1 1 100%"><span class="cfg-label">名称（显示名，亦作唯一 ID，建议英文）</span>
        <input id="bfName" class="input" value="${esc(b ? b.name : "")}" placeholder="如 my_bot" ${b ? "disabled" : ""}></label>
      <label class="cfg-field" style="flex:1 1 100%"><span class="cfg-label">人格设定（留空=沿用基座人设）</span>
        <textarea id="bfPersona" class="textarea" rows="4">${esc(b && b.persona ? b.persona : "")}</textarea></label>
      <label class="cfg-field"><span class="cfg-label">模型覆盖（留空=全局默认）</span>
        <input id="bfModel" class="input" value="${esc(b && b.model ? b.model : "")}" placeholder="如 deepseek-chat"></label>
      <label class="cfg-field"><span class="cfg-label">插件（空=全部，逗号分隔名称）</span>
        <input id="bfPlugins" class="input" value="${esc(b && b.plugins ? b.plugins.join(",") : "")}"></label>
      <label class="cfg-field cfg-check"><input type="checkbox" id="bfAuto" ${b && b.autostart ? "checked" : (!b ? "checked" : "")}> 随应用自启</label>
    </div>
    <div class="row-actions" style="margin-top:10px">
      <button class="btn primary" id="bfSave">${b ? "保存" : "创建"}</button>
      <span class="hint" id="bfMsg"></span>
    </div>`;
  $("#bfSave").onclick = () => b ? botUpdate(id) : botCreate();
}

function _botFormData() {
  const name = ($("#bfName").value || "").trim();
  const persona = ($("#bfPersona").value || "").trim() || null;
  const model = ($("#bfModel").value || "").trim() || null;
  const pluginsRaw = ($("#bfPlugins").value || "").trim();
  const plugins = pluginsRaw ? pluginsRaw.split(",").map(s => s.trim()).filter(Boolean) : null;
  const autostart = !!$("#bfAuto").checked;
  return { name, persona, model, plugins, autostart };
}

async function botCreate() {
  const d = _botFormData();
  if (!d.name) { $("#bfMsg").textContent = "请填写名称"; return; }
  const r = await PB("/api/bots/create", d);
  if (r && r.ok !== false) {
    state.botId = d.name;
    await loadBots(); onBotSwitched(); $("#botModal").classList.add("hidden");
    toast("已创建并切换至 " + d.name);
  } else $("#bfMsg").textContent = "创建失败：" + (r && r.error || "未知");
}

async function botUpdate(id) {
  const d = _botFormData();
  const r = await PB("/api/bots/update", { id, persona: d.persona, model: d.model, plugins: d.plugins, autostart: d.autostart });
  if (r && r.ok !== false) { await loadBots(); openBotModal(); toast("已保存"); }
  else $("#bfMsg").textContent = "保存失败：" + (r && r.error || "未知");
}

async function botToggle(id) {
  const b = state.bots.find(x => x.id === id);
  const r = await PB(b && b.running ? "/api/bots/stop" : "/api/bots/start", { id });
  if (r && r.ok !== false) { await loadBots(); openBotModal(); onBotSwitched(); }
  else toast("操作失败：" + (r && r.error || "未知"), true);
}

async function botDelete(id) {
  if (!confirm(`确定删除 Bot「${id}」？其记忆 / 配置将一并清除（不可恢复）。`)) return;
  const r = await PB("/api/bots/delete", { id });
  if (r && r.ok !== false) {
    if (state.botId === id) state.botId = state.defaultBot;
    await loadBots(); onBotSwitched(); $("#botModal").classList.add("hidden");
    toast("已删除 " + id);
  } else toast("删除失败：" + (r && r.error || "未知"), true);
}

/* ---------------- 插件中心（插件市场 + 插件包 + 依赖分组） ---------------- */
const KIND_NAME = { platform: "平台", feature: "功能", brain: "大脑", sidecar: "Sidecar", local: "本地" };

async function loadMarket() {
  const body = $("#tblMarket tbody");
  const empty = $("#marketEmpty");
  const sum = $("#marketSummary");
  try {
    const d = await GET("/api/plugins/market");
    if (!d.ok || !d.repo_exists) {
      body.innerHTML = "";
      empty.textContent = d.repo_exists
        ? "仓库为空。把插件包 / 资源包放入仓库目录后点「刷新市场」。"
        : `未找到插件仓库（期望位于 ${d.repo || "<盘>:\\plugins"}）。` +
          "把插件包放入仓库 plugins/ 子目录、资源包放入仓库根目录后，这里即可一键安装。";
      sum.textContent = "";
      return;
    }
    empty.textContent = `仓库位置：${d.repo} · 插件装入本包插件库，资源大件以联接挂入（不复制）`;
    const items = d.items || [];
    const installed = items.filter(i => i.installed).length;
    sum.textContent = `${items.length} 项可提供 · 已安装 ${installed}`;
    body.innerHTML = items.map(it => {
      const size = it.kind === "pack"
        ? (it.size_gb >= 1 ? it.size_gb + " GB" : Math.round(it.size_gb * 1024) + " MB")
        : (it.size_mb >= 1 ? it.size_mb + " MB" : it.size_mb * 1024 + " KB");
      const kind = it.kind === "pack" ? "资源" : "插件";
      const btn = it.installed
        ? `<button class="btn ghost act-market" data-name="${esc(it.name)}" data-act="uninstall">卸载</button>`
        : `<button class="btn primary act-market" data-name="${esc(it.name)}" data-act="install">安装</button>`;
      return `<tr>
        <td>${esc(it.title)}<div class="hint">${esc(it.name)} · ${kind}</div></td>
        <td class="hint">${esc(it.description || "")}</td>
        <td class="hint">${size}</td>
        <td>${btn}</td></tr>`;
    }).join("");
    $$("#tblMarket .act-market").forEach(b => b.addEventListener("click", async () => {
      const name = b.dataset.name, act = b.dataset.act;
      b.disabled = true;
      b.textContent = act === "install" ? "安装中…" : "卸载中…";
      try {
        const r = await POST("/api/plugins/market", { action: act, name });
        toast(r.ok ? (r.hint || "完成") : (r.error || "失败"), !r.ok);
      } catch (e) { toast(e.message, true); }
      await loadMarket();
      await loadPlugins();
    }));
  } catch (e) {
    body.innerHTML = "";
    empty.textContent = "市场加载失败：" + e.message;
  }
}

$("#btnRefreshMarket").addEventListener("click", loadMarket);

$("#btnSeek").addEventListener("click", async () => {
  const btn = $("#btnSeek"), box = $("#seekResult");
  btn.disabled = true;
  btn.textContent = "全盘扫描中…（可能需要 10~60 秒）";
  box.classList.remove("hidden");
  box.textContent = "正在扫描所有本地磁盘，寻找可接入的插件与资源包…";
  try {
    const r = await POST("/api/plugins/seek", {});
    const parts = [];
    if (r.installed?.length) parts.push("新安装：" + r.installed.join("、"));
    if (r.skipped?.length) parts.push("已存在：" + r.skipped.join("、"));
    if (r.failed?.length) parts.push("失败：" + r.failed.join("、"));
    if (!parts.length) parts.push("未发现可接入的插件或资源包");
    box.innerHTML = `<b>扫描完成${r.timeout ? "（超时截断，可再扫一次）" : ""}</b>` +
      (r.found?.plugins?.length || r.found?.packs?.length
        ? ` · 发现插件 ${r.found.plugins.length} 个、资源包 ${r.found.packs.length} 个` : "") +
      "<br>" + parts.map(esc).join("<br>");
    toast(r.installed?.length ? `已自动安装 ${r.installed.length} 项` : "扫描完成，未发现新插件", !r.installed?.length);
  } catch (e) {
    box.textContent = "扫描失败：" + e.message;
    toast(e.message, true);
  }
  btn.disabled = false;
  btn.textContent = "寻找插件";
  await loadMarket();
  await loadPlugins();
});

async function loadPlugins() {
  loadMarket().catch(() => {});
  const d = await GET("/api/plugins");
  state.plugins = d;
  const items = d.items || [];
  const en = items.filter(i => i.enabled).length;
  $("#pkgSummary").textContent =
    `共 ${items.length} 个插件包 · 已启用 ${en} 个` + (d.core_running ? "" : " · 核心未启动（启停将在核心启动时生效）");

  // 按依赖分组渲染：core 必装组置顶展示（不可整体卸载），其余组带批量启停
  const groups = d.groups || {};
  const byGroup = {};
  const grouped = new Set();
  Object.entries(groups).forEach(([g, meta]) =>
    (meta.packages || []).forEach(name => { byGroup[name] = g; grouped.add(name); }));

  const body = $("#pkgGroups");
  body.innerHTML = "";
  const order = Object.keys(groups);
  // 未分组插件兜底进「其它」
  const ungrouped = items.filter(p => !grouped.has(p.name));
  for (const g of order) {
    const meta = groups[g];
    const members = items.filter(p => byGroup[p.name] === g);
    if (!members.length && g !== "core") continue;
    const allOn = members.length && members.every(p => p.enabled);
    body.appendChild(groupSection(g, meta, members, allOn));
  }
  if (ungrouped.length) {
    body.appendChild(groupSection("__other", { title: "其它", description: "未归组插件" }, ungrouped, false));
  }

  bindPluginEvents();
}

function groupSection(g, meta, members, allOn) {
  const sec = document.createElement("div");
  sec.className = "panel";
  const desc = meta.description ? `<div class="hint" style="margin-bottom:10px">${esc(meta.description)}</div>` : "";
  const bulk = g === "core" ? `<span class="badge">基础</span>` :
    `<button class="btn small" data-grp="${esc(g)}" data-act="on">整组启用</button>
     <button class="btn small" data-grp="${esc(g)}" data-act="off">整组停用</button>`;
  sec.innerHTML = `
    <div class="panel-title" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span>${esc(meta.title || g)}</span>
      <span class="badge">${members.filter(p => p.enabled).length}/${members.length} 启用</span>
      ${bulk}
    </div>${desc}<div class="table-wrap"><table><thead>
      <tr><th>启用</th><th>名称</th><th>类型</th><th>运行</th><th>操作</th></tr>
    </thead><tbody class="grp-body"></tbody></table></div>`;
  const tb = $(".grp-body", sec);
  tb.innerHTML = members.map(p => {
    const dep = (p.requires || []).length ? ` · 依赖 ${esc(p.requires.join(", "))}` : "";
    const miss = (p.missing_deps || []).length
      ? `<div class="hint" style="color:var(--warn)">缺依赖: ${esc(p.missing_deps.join(", "))}</div>` : "";
    let run = "", ops = "";
    if (p.kind === "brain") {
      run = p.running ? "已启动" : (p.enabled ? "已注册待命" : "未注册");
      ops = `<button class="btn small act-pkg-brain" data-name="${esc(p.name)}" data-act="start">启动</button>
             <button class="btn small act-pkg-brain" data-name="${esc(p.name)}" data-act="stop">停止</button>
             <button class="btn small act-pkg-brain" data-name="${esc(p.name)}" data-act="status">状态</button>`;
    } else if (p.kind === "sidecar") {
      run = p.running ? `运行中 :${(p.detail && p.detail.port) || ""}` : "未运行";
      ops = `<button class="btn small act-pkg-sc" data-name="${esc(p.name)}" data-act="start">启动</button>
             <button class="btn small act-pkg-sc" data-name="${esc(p.name)}" data-act="stop">停止</button>
             <button class="btn small act-pkg-sc" data-name="${esc(p.name)}" data-act="restart">重启</button>`;
    } else {
      run = p.running ? "运行中" : (p.enabled ? "已装载" : "未装载");
    }
    const cfgBtn = p.has_config
      ? `<button class="btn small act-pkg-cfg" data-name="${esc(p.name)}">设置</button>` : "";
    ops = (ops ? ops + " " : "") + cfgBtn;
    return `<tr>
      <td><label class="switch"><input type="checkbox" data-pkg="${esc(p.name)}" ${p.enabled ? "checked" : ""}><span class="track"></span></label></td>
      <td><b>${esc(p.title)}</b><div class="hint">${esc(p.name)} v${esc(p.version)}${dep}</div>${miss}</td>
      <td><span class="badge">${KIND_NAME[p.kind] || p.kind}</span></td>
      <td>${run}</td>
      <td>${ops}</td>
      <td class="hint wrap" style="display:none">${esc(p.description || "")}</td></tr>`;
  }).join("");
  return sec;
}

function bindPluginEvents() {
  $$("#pkgGroups input[type=checkbox]").forEach(cb => cb.addEventListener("change", async () => {
    const name = cb.dataset.pkg, on = cb.checked;
    cb.disabled = true;
    try {
      const r = await POST("/api/plugins/toggle", { name, on });
      toast(r.ok ? (r.hint || (on ? "已装载" : "已卸载")) : (r.error || "失败"), !r.ok);
      await refreshStatus();
      await loadPlugins();
    } catch (e) { toast(e.message, true); cb.checked = !on; }
    cb.disabled = false;
  }));
  $$("#pkgGroups .act-pkg-brain").forEach(b => b.addEventListener("click", async () => {
    const r = await POST("/api/plugins/brain", { name: b.dataset.name, action: b.dataset.act });
    toast(r.ok ? (b.dataset.act === "status" ? JSON.stringify(r.status).slice(0, 110) : "已完成") : (r.error || "失败"), !r.ok);
    await refreshStatus(); await loadPlugins();
  }));
  $$("#pkgGroups .act-pkg-sc").forEach(b => b.addEventListener("click", async () => {
    const r = await POST("/api/plugins/sidecar", { name: b.dataset.name, action: b.dataset.act });
    toast(r.ok ? "已完成" : (r.error || "失败"), !r.ok);
    await loadPlugins();
  }));
  $$("#pkgGroups [data-grp]").forEach(b => b.addEventListener("click", async () => {
    const grp = b.dataset.grp, act = b.dataset.act;
    b.disabled = true; b.textContent = act === "on" ? "启用中…" : "停用中…";
    try {
      const r = await POST("/api/plugins/group", { group: grp, action: act });
      if (r.ok) {
        const bad = Object.values(r.results || {}).filter(x => !x.ok).length;
        toast(bad ? `完成，${bad} 个失败` : "整组操作完成", bad > 0);
      } else toast(r.error || "失败", true);
    } catch (e) { toast(e.message, true); }
    await refreshStatus(); await loadPlugins();
  }));
  $$("#pkgGroups .act-pkg-cfg").forEach(b => b.addEventListener("click", () => openPluginConfig(b.dataset.name)));
}

// ---------------- 插件扩展设置（config_schema 动态表单） ----------------
let _cfgName = null;

async function openPluginConfig(name) {
  _cfgName = name;
  $("#cfgBody").innerHTML = "加载中…";
  $("#cfgModal").classList.remove("hidden");
  try {
    const d = await GET(`/api/plugins/${encodeURIComponent(name)}/config`);
    if (d.error) {
      $("#cfgBody").innerHTML = `<div class="hint" style="color:var(--warn)">${esc(d.error)}</div>`;
      return;
    }
    $("#cfgTitle").textContent = `${d.display_name || name} · 扩展设置`;
    $("#cfgBody").innerHTML = d.fields && d.fields.length
      ? `<div class="form-grid">${d.fields.map(configField).join("")}</div>`
      : `<div class="hint">该插件暂无可配置项。</div>`;
  } catch (e) { $("#cfgBody").innerHTML = `<div class="hint" style="color:var(--warn)">${esc(e.message)}</div>`; }
}

function configField(f) {
  const id = "cfg_" + f.key;
  const t = f.type || "str";
  const v = f.value;
  let control = "";
  if (t === "bool") {
    control = `<label class="switch"><input type="checkbox" id="${id}" ${v ? "checked" : ""}><span class="track"></span></label>`;
  } else if (t === "choice") {
    control = `<select id="${id}" class="select">` +
      (f.options || []).map(o => `<option value="${esc(o.value)}" ${String(o.value) === String(v) ? "selected" : ""}>${esc(o.label != null ? o.label : o.value)}</option>`).join("") +
      `</select>`;
  } else if (t === "secret") {
    control = `<input id="${id}" class="input" type="${f.masked ? "password" : "text"}" placeholder="${f.masked ? "已设置（留空或不改则保持原值）" : "请输入"}" value="${esc(f.masked ? v : (v == null ? "" : v))}">`;
  } else if (t === "json") {
    control = `<textarea id="${id}" class="textarea" rows="4">${esc(typeof v === "string" ? v : JSON.stringify(v ?? ""))}</textarea>`;
  } else if (t === "text") {
    control = `<textarea id="${id}" class="textarea" rows="3">${esc(v == null ? "" : v)}</textarea>`;
  } else {
    const tp = (t === "int" || t === "float") ? "number" : "text";
    const step = t === "int" ? "step=1" : (t === "float" ? "step=0.01" : "");
    control = `<input id="${id}" class="input" type="${tp}" ${step} value="${esc(v == null ? "" : v)}">`;
  }
  const desc = f.desc ? `<span class="hint">${esc(f.desc)}</span>` : "";
  return `<label class="cfg-field"><span class="cfg-label">${esc(f.label || f.key)}</span>${desc}<div class="cfg-control">${control}</div></label>`;
}

async function savePluginConfig() {
  const name = _cfgName;
  if (!name) return;
  let d0;
  try { d0 = await GET(`/api/plugins/${encodeURIComponent(name)}/config`); }
  catch (e) { toast(e.message, true); return; }
  const fields = d0.fields || [];
  const values = {};
  for (const f of fields) {
    const el = document.getElementById("cfg_" + f.key);
    if (!el) continue;
    const t = f.type || "str";
    let val;
    if (t === "bool") val = el.checked;
    else if (t === "json") { try { val = JSON.parse(el.value || "{}"); } catch { val = el.value; } }
    else val = el.value;
    values[f.key] = val;
  }
  try {
    const r = await POST(`/api/plugins/${encodeURIComponent(name)}/config`, { values });
    if (r.ok) { toast("已保存" + (r.applied ? "（已热生效）" : "（下次启用/重载生效）")); closeCfgModal(); await loadPlugins(); }
    else toast(r.error || "保存失败", true);
  } catch (e) { toast(e.message, true); }
}

function closeCfgModal() { $("#cfgModal").classList.add("hidden"); _cfgName = null; }

$("#btnRescan").addEventListener("click", async () => {
  try {
    const r = await POST("/api/plugins/rescan");
    toast(`扫描完成，发现 ${(r.packages || []).length} 个插件包`);
    await loadPlugins();
  } catch (e) { toast(e.message, true); }
});

/* ---------------- 配置中心 ---------------- */
async function loadConfig() {
  const d = await GET("/api/config");
  state.config = d;
  $("#configBody").innerHTML = d.sections.map(sec => `<div class="cfg-section">
    <div class="cfg-title">${esc(sec.section)}</div>
    <div class="cfg-hint">${esc(sec.hint || "")}</div>
    <div class="cfg-grid">${sec.items.map(it => cfgItem(it)).join("")}</div></div>`).join("");

  $$("#configBody input[data-key], #configBody textarea[data-key], #configBody select[data-key]").forEach(inp => {
    inp.dataset.dirty = "";
    inp.addEventListener("input", () => { inp.dataset.dirty = "1"; });
    if (inp.tagName === "SELECT") inp.addEventListener("change", () => { inp.dataset.dirty = "1"; });
  });
  renderBotConfigPanel();
  loadProviders().catch(e => console.warn("loadProviders", e));
  mountIssues($("#issueMountConfig"));
}

/* 当前 Bot 专属配置（人格 / 模型 / 插件 / 自启），随 Bot 切换而切换 */
function renderBotConfigPanel() {
  const b = state.bots.find(x => x.id === state.botId)
    || { id: state.defaultBot, name: "肥鱼娘", persona: null, model: null, plugins: null, autostart: true, running: false };
  const el = $("#botConfigPanel");
  if (!el) return;
  el.innerHTML = `<div class="panel-title">当前 Bot 专属配置：${esc(b.name || b.id)}
      <span class="badge ${b.running ? "ok" : "off"}">${b.running ? "运行中" : "已停止"}</span></div>
    <div class="hint wrap" style="margin-bottom:10px">以下配置仅作用于当前选中的 Bot（${esc(b.id)}）。切换上方 Bot 后会显示对应配置；下方「全局配置 / AI 供应商」对所有 Bot 共享。</div>
    <div class="cfg-grid">
      <label class="cfg-field" style="flex:1 1 100%"><span class="cfg-label">人格设定（留空=沿用基座人设）</span>
        <textarea id="botPersona" class="textarea" rows="4">${esc(b.persona || "")}</textarea></label>
      <label class="cfg-field"><span class="cfg-label">模型覆盖（留空=全局默认）</span>
        <input id="botModel" class="input" value="${esc(b.model || "")}" placeholder="如 deepseek-chat"></label>
      <label class="cfg-field"><span class="cfg-label">插件列表（空=全部；逗号分隔名称）</span>
        <input id="botPlugins" class="input" value="${esc((b.plugins || []).join(","))}"></label>
      <label class="cfg-field cfg-check"><input type="checkbox" id="botAutostart" ${b.autostart ? "checked" : ""}> 随应用自启</label>
    </div>
    <div class="row-actions" style="margin-top:10px">
      <button class="btn primary" id="btnSaveBotConfig">保存 Bot 配置</button>
      <button class="btn ghost" id="btnBotStartStop">${b.running ? "停止" : "启动"}</button>
      <span class="hint" id="botConfigMsg"></span>
    </div>`;
  $("#btnSaveBotConfig").onclick = saveBotConfig;
  $("#btnBotStartStop").onclick = () => botToggle(b.id);
}

async function saveBotConfig() {
  const id = state.botId;
  const persona = ($("#botPersona").value || "").trim() || null;
  const model = ($("#botModel").value || "").trim() || null;
  const pluginsRaw = ($("#botPlugins").value || "").trim();
  const plugins = pluginsRaw ? pluginsRaw.split(",").map(s => s.trim()).filter(Boolean) : null;
  const autostart = !!$("#botAutostart").checked;
  const r = await PB("/api/bots/update", { id, persona, model, plugins, autostart });
  if (r && r.ok !== false) {
    toast("已保存 Bot 配置（运行中的 Bot 将自动重启生效）");
    await loadBots();
    renderBotConfigPanel();
  } else toast("保存失败：" + (r && r.error || "未知"), true);
}

/* ---------------- AI 供应商 / 模型管理（注册表） ---------------- */
let _provPresets = {};

function _provFillModels(models, selected) {
  const sel = $("#provModel");
  sel.innerHTML = '<option value="">— 选择模型 —</option>';
  (models || []).forEach(m => {
    const o = document.createElement("option");
    o.value = m; o.textContent = m;
    if (m === selected) o.selected = true;
    sel.appendChild(o);
  });
  const custom = document.createElement("option");
  custom.value = "__custom__"; custom.textContent = "✎ 自定义（在下方填写）";
  sel.appendChild(custom);
}

function _provApplyPreset(id) {
  const p = _provPresets[id];
  if (!p) return;
  $("#provStyle").value = p.api_style || "openai";
  $("#provBaseUrl").value = p.base_url || "";
  _provFillModels(p.models, "");
  $("#provCustomModel").value = "";
}

const CAP_LABELS = { chat: "对话", reasoning: "推理", vision: "视觉", role: "角色" };

function _provCardHTML(m) {
  const caps = m.capabilities || [];
  const tags = caps.map(c => {
    const on = m.active && m.active[c];
    return `<span class="tag ${on ? "on" : ""}">${CAP_LABELS[c] || c}${on ? "·默认" : ""}</span>`;
  }).join(" ");
  const setBtns = caps.map(c =>
    `<button class="btn ghost" data-act="set" data-name="${esc(m.name)}" data-cap="${c}">设为${CAP_LABELS[c] || c}默认</button>`
  ).join("");
  return `<div class="prov-card" data-name="${esc(m.name)}">
    <div class="prov-card-head">
      <span class="prov-card-name">${esc(m.name)}</span>
      <span class="prov-card-meta">${esc((_provPresets[m.preset] || {}).label || m.preset || "自定义")} · ${esc(m.model)}</span>
    </div>
    <div class="prov-card-head" style="margin-top:6px">${tags || '<span class="tag">无能力</span>'}</div>
    <div class="prov-card-actions">
      ${setBtns}
      <button class="btn ghost" data-act="edit" data-name="${esc(m.name)}">编辑</button>
      <button class="btn danger" data-act="del" data-name="${esc(m.name)}">删除</button>
    </div>
  </div>`;
}

async function loadProviders() {
  const d = await GET("/api/providers");
  _provPresets = d.presets || {};
  const sel = $("#provPreset");
  sel.innerHTML = '<option value="">— 选择厂商 —</option>';
  Object.keys(_provPresets).forEach(id => {
    const o = document.createElement("option");
    o.value = id; o.textContent = _provPresets[id].label || id;
    sel.appendChild(o);
  });
  const list = $("#provList");
  if (!d.models || !d.models.length) {
    list.innerHTML = '<div class="hint">暂无已保存的模型。在下方填入厂商/Key/模型名并勾选能力，点「保存并设为默认」即可添加。</div>';
  } else {
    list.innerHTML = d.models.map(_provCardHTML).join("");
  }
  $("#provMsg").textContent = "";
}

async function _provSave() {
  const preset = $("#provPreset").value;
  const model = $("#provModel").value === "__custom__" ? "" : $("#provModel").value;
  const caps = ["chat", "reasoning", "vision", "role"].filter(c => $("#provCap" + c[0].toUpperCase() + c.slice(1)).checked);
  const payload = {
    name: $("#provName").value.trim(),
    preset,
    api_style: $("#provStyle").value,
    base_url: $("#provBaseUrl").value.trim(),
    api_key: $("#provKey").value,
    model,
    custom_model: $("#provCustomModel").value.trim(),
    capabilities: caps,
  };
  if (!payload.name) { toast("请填写「模型名称」", true); return; }
  const btn = $("#btnSaveProvider");
  btn.disabled = true;
  try {
    const r = await POST("/api/providers", payload);
    if (!r.ok) throw new Error(r.error || "保存失败");
    $("#provMsg").textContent = "已保存并热重载 ✓ " + payload.name + "（" + (caps.map(c => CAP_LABELS[c]).join("/") || "无能力") + "）";
    toast("模型已保存并设为默认");
    await loadProviders();
  } catch (e) {
    toast(e.message, true);
    $("#provMsg").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

async function _provActivate(name, cap) {
  try {
    const r = await POST("/api/providers/activate", { name, capability: cap });
    if (!r.ok) throw new Error(r.error || "切换失败");
    toast(name + " 已设为" + (CAP_LABELS[cap] || cap) + "默认");
    await loadProviders();
  } catch (e) { toast(e.message, true); }
}

async function _provDelete(name) {
  if (!confirm("确定删除模型「" + name + "」？")) return;
  try {
    const r = await POST("/api/providers/delete", { name });
    if (!r.ok) throw new Error(r.error || "删除失败");
    toast("已删除 " + name);
    await loadProviders();
  } catch (e) { toast(e.message, true); }
}

function _provEdit(name) {
  GET("/api/providers").then(d => {
    const m = (d.models || []).find(x => x.name === name);
    if (!m) return;
    $("#provName").value = m.name;
    $("#provPreset").value = _provPresets[m.preset] ? m.preset : "";
    $("#provStyle").value = m.api_style || "openai";
    $("#provBaseUrl").value = m.base_url || "";
    _provFillModels((_provPresets[m.preset] || {}).models || [], m.model);
    if (m.model) $("#provCustomModel").value = m.model;
    $("#provKey").value = "";  // 不回显密钥
    ["chat", "reasoning", "vision", "role"].forEach(c => {
      $("#provCap" + c[0].toUpperCase() + c.slice(1)).checked = (m.capabilities || []).includes(c);
    });
    $("#provMsg").textContent = "正在编辑：" + m.name + "（改 Key 留空即不修改）";
    $("#provPanel").scrollIntoView({ behavior: "smooth" });
  }).catch(e => toast(e.message, true));
}

function cfgItem(it) {
  const key = esc(it.key), label = esc(it.label);
  if (it.type === "bool") {
    return `<div class="cfg-bool" data-key="${key}">
      <label class="switch"><input type="checkbox" data-key="${key}" data-type="bool" ${it.value ? "checked" : ""}><span class="track"></span></label>
      <span class="cfg-label">${label}</span></div>`;
  }
  if (it.type === "text") {
    return `<div class="cfg-item" style="grid-column: 1/-1"><span class="cfg-label">${label} <span class="hint">(${key})</span></span>
      <textarea data-key="${key}" data-type="text">${esc(it.value)}</textarea></div>`;
  }
  if (it.type === "secret") {
    return `<div class="cfg-item"><span class="cfg-label">${label} <span class="hint">(${key}${it.masked ? "，已配置" : ""})</span></span>
      <input data-key="${key}" data-type="secret" value="${esc(it.value)}" placeholder="留掩码表示不修改"></div>`;
  }
  if (it.type === "select") {
    const opts = (it.options || []).map(o =>
      `<option value="${esc(o)}" ${o === it.value ? "selected" : ""}>${esc(o)}</option>`).join("");
    return `<div class="cfg-item"><span class="cfg-label">${label} <span class="hint">(${key})</span></span>
      <select data-key="${key}" data-type="select" class="select">${opts}</select></div>`;
  }
  return `<div class="cfg-item"><span class="cfg-label">${label} <span class="hint">(${key})</span></span>
    <input data-key="${key}" data-type="${esc(it.type)}" value="${esc(it.value)}"></div>`;
}

$("#btnSaveConfig").addEventListener("click", async () => {
  const values = {};
  $$("#configBody [data-key]").forEach(inp => {
    if (inp.dataset.dirty !== "1") return;
    const k = inp.dataset.key;
    values[k] = inp.type === "checkbox" ? inp.checked : inp.value;
  });
  if (!Object.keys(values).length) return toast("没有修改过的配置项");
  const btn = $("#btnSaveConfig");
  btn.disabled = true;
  try {
    const r = await POST("/api/config/save", { values });
    if (r.errors && r.errors.length) toast("部分失败: " + r.errors.join("; "), true);
    else toast("已保存" + (r.provider_reload ? "，供应商已热重载" : "，立即生效"));
    await loadConfig();
  } catch (e) { toast(e.message, true); }
  btn.disabled = false;
});

/* ---------------- 智能体自编程 Issue 管理（可挂载到任意容器） ---------------- */
function mountIssues(container) {
  if (!container) return;
  container.innerHTML = `<div class="panel">
    <div class="panel-title">智能体自编程 · Issue 队列</div>
    <div class="hint wrap is-status" style="margin-bottom:10px"></div>
    <div class="sc-list is-list"></div>
    <div class="row-actions" style="margin-top:12px">
      <input class="input is-title" placeholder="提一个需求 / 卡点给智能体（例：加一个每日总结大脑）" style="flex:1 1 320px">
      <button class="btn ghost is-refresh">刷新</button>
      <button class="btn primary is-file">提交 Issue</button>
    </div></div>`;
  const statusEl = container.querySelector(".is-status");
  const listEl = container.querySelector(".is-list");
  const titleEl = container.querySelector(".is-title");

  async function refresh() {
    try {
      const d = await GET("/api/self_coding/issues");
      const enabled = !!d.enabled;
      statusEl.innerHTML = `自编程总开关：<b style="color:${enabled ? "var(--ok)" : "var(--warn)"}">`
        + `${enabled ? "已开启" : "未开启"}</b> ｜ 开关与权限档在「配置」页的「智能体自编程」区；`
        + `Issue 默认自动执行，关闭后需在此点「同意」才构建。`;
      const issues = d.issues || [];
      if (!issues.length) {
        listEl.innerHTML = `<div class="hint">暂无 Issue。智能体受阻或想要新功能时会自动提；也可在下方输入框手动提交。</div>`;
        return;
      }
      const stateName = { open: "执行中", pending: "待批准", done: "已完成", failed: "失败", rejected: "已拒绝" };
      listEl.innerHTML = issues.map(it => {
        const cls = (it.state || "").replace(/[^a-z]/g, "");
        const actions = it.state === "pending"
          ? `<button class="btn ghost sm" data-act="approve" data-id="${esc(it.id)}">同意</button>`
            + `<button class="btn ghost sm" data-act="reject" data-id="${esc(it.id)}">拒绝</button>`
          : `<span class="hint">轮次 ${it.rounds || 0}${it.updated ? " · " + it.updated : ""}</span>`;
        return `<div class="sc-issue">
          <div class="sc-issue-head"><span class="badge">${esc(it.kind || "")}</span>`
          + `<span class="badge ${esc(cls)}">${stateName[it.state] || it.state || ""}</span>`
          + `<b>${esc(it.id)}</b> <span class="hint">${esc(it.title || "")}</span></div>`
          + (it.result ? `<div class="hint wrap" style="margin:4px 0">结果：${esc(it.result)}</div>` : "")
          + `<div class="row-actions">${actions}</div></div>`;
      }).join("");
      listEl.querySelectorAll("button[data-act]").forEach(b => b.onclick = () => {
        const id = b.dataset.id, act = b.dataset.act;
        (act === "approve" ? POST("/api/self_coding/approve", { id })
                           : POST("/api/self_coding/reject", { id }))
          .then(r => {
            toast(r && r.ok !== false ? (act === "approve" ? "已派给构建助手执行~" : "已拒绝")
                                       : "失败：" + ((r && r.error) || ""), !(r && r.ok !== false));
            refresh();
          })
          .catch(e => toast(e.message, true));
      });
    } catch (e) {
      listEl.innerHTML = `<div class="hint" style="color:var(--warn)">${esc(e.message)}</div>`;
    }
  }

  container.querySelector(".is-refresh").onclick = refresh;
  container.querySelector(".is-file").onclick = async () => {
    const title = (titleEl.value || "").trim();
    if (!title) return toast("请先填写需求 / 卡点");
    try {
      const r = await POST("/api/self_coding/file", { title, kind: "feature" });
      if (r && r.ok !== false) {
        toast(r.auto ? `已提交并自动执行（${r.issue_id}）` : `已提交，待批准（${r.issue_id}）`);
        titleEl.value = "";
        refresh();
      } else toast("提交失败：" + ((r && r.error) || ""), true);
    } catch (e) { toast(e.message, true); }
  };
  refresh();
}

function loadIssuesPage() {
  mountIssues($("#issueMountPage"));
}

// ----- AI 供应商 / 模型管理 -----
$("#provList").addEventListener("click", e => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const name = btn.dataset.name, act = btn.dataset.act;
  if (act === "set") _provActivate(name, btn.dataset.cap);
  else if (act === "del") _provDelete(name);
  else if (act === "edit") _provEdit(name);
});
$("#provPreset").addEventListener("change", _provApplyPreset);
$("#btnSaveProvider").addEventListener("click", _provSave);

/* ---------------- 外观设置（主题色 + 窗口标题） ---------------- */
const appearance = { theme: "midnight", title: "肥鱼娘 · App 控制台", themes: {}, vars: {} };

function applyVars(vars) {
  const root = document.documentElement;
  for (const k in (vars || {})) root.style.setProperty(k, vars[k]);
}
function brandShort(t) {
  const part = String(t || "").split(/[·•・]/)[0].trim();
  return part || t || "肥鱼娘";
}
function applyTitle(t) {
  document.title = t;
  const b = $("#brandName");
  if (b) b.textContent = brandShort(t);
}

async function loadAppearance() {
  try {
    const d = await GET("/api/appearance");
    appearance.theme = d.theme; appearance.title = d.title; appearance.bg = d.bg || "";
    appearance.bg_url = d.bg_url || "";
    appearance.themes = d.themes || {}; appearance.vars = d.vars || {};
    applyVars(appearance.vars);
    applyTitle(d.title);
    applyBg(appearance.bg_url);
    setBgPreview(appearance.bg_url);
    renderThemeGrid();
    $("#titleInput").value = d.title;
    $("#titlePreview").textContent = d.title;
  } catch (e) { /* 兜底用 CSS 默认主题 */ }
}

function applyBg(url) {
  const body = document.body, dim = $("#bgDim");
  if (!url) {
    body.style.backgroundImage = "";
    if (dim) dim.style.display = "none";
    return;
  }
  body.style.backgroundImage = `url("${url}")`;
  body.style.backgroundSize = "cover";
  body.style.backgroundPosition = "center";
  body.style.backgroundAttachment = "fixed";
  if (dim) dim.style.display = "block";
}

function setBgPreview(url) {
  const el = $("#bgPreview");
  if (!el) return;
  if (!url) { el.style.display = "none"; el.style.backgroundImage = ""; return; }
  el.style.display = "block";
  el.style.backgroundImage = `url("${url}")`;
}

async function uploadBg(file) {
  const msg = $("#bgMsg");
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) { msg.textContent = "图片过大，上限 8MB"; return; }
  msg.textContent = "上传中…";
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const r = await POST("/api/appearance", { bg_data: reader.result });
      appearance.bg = r.bg; appearance.bg_url = r.bg_url || "";
      applyBg(appearance.bg_url); setBgPreview(appearance.bg_url);
      msg.textContent = "背景已应用";
    } catch (e) { msg.textContent = (e.message || "上传失败"); }
  };
  reader.onerror = () => { msg.textContent = "读取文件失败"; };
  reader.readAsDataURL(file);
}

$("#bgFile").addEventListener("change", e => {
  const f = e.target.files && e.target.files[0];
  uploadBg(f);
  e.target.value = "";
});

$("#btnApplyBgUrl").addEventListener("click", async () => {
  const msg = $("#bgMsg");
  const url = $("#bgUrl").value.trim();
  if (!url) { msg.textContent = "请先填写图片链接"; return; }
  msg.textContent = "应用中…";
  try {
    const r = await POST("/api/appearance", { bg: url });
    appearance.bg = r.bg; appearance.bg_url = r.bg_url || "";
    applyBg(appearance.bg_url); setBgPreview(appearance.bg_url);
    msg.textContent = "背景已应用";
  } catch (e) { msg.textContent = (e.message || "应用失败"); }
});

$("#btnClearBg").addEventListener("click", async () => {
  const msg = $("#bgMsg");
  msg.textContent = "清除中…";
  try {
    const r = await POST("/api/appearance", { clear_bg: true });
    appearance.bg = r.bg; appearance.bg_url = r.bg_url || "";
    applyBg(appearance.bg_url); setBgPreview(appearance.bg_url);
    $("#bgUrl").value = "";
    msg.textContent = "已清除背景";
  } catch (e) { msg.textContent = (e.message || "清除失败"); }
});

function setFaviconHref(href) {
  let link = document.querySelector('link[rel="icon"][href^="/static/icon"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }
  link.href = href;
}

async function uploadIcon(file) {
  const msg = $("#iconMsg");
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) { msg.textContent = "图片过大，上限 8MB"; return; }
  msg.textContent = "上传中…";
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const r = await POST("/api/appearance/icon", { icon_data: reader.result });
      const ts = r.ts || Date.now();
      $("#iconPreview").src = "/static/icon.png?t=" + ts;
      setFaviconHref("/static/icon.png?t=" + ts);
      msg.textContent = "图标已更新（标签页/任务栏即时生效，独立窗口需重启）";
    } catch (e) { msg.textContent = (e.message || "上传失败"); }
  };
  reader.onerror = () => { msg.textContent = "读取文件失败"; };
  reader.readAsDataURL(file);
}

$("#iconFile").addEventListener("change", e => {
  const f = e.target.files && e.target.files[0];
  uploadIcon(f);
  e.target.value = "";
});

$("#btnResetIcon").addEventListener("click", async () => {
  const msg = $("#iconMsg");
  msg.textContent = "恢复中…";
  try {
    const r = await POST("/api/appearance/icon/reset", {});
    const ts = r.ts || Date.now();
    $("#iconPreview").src = "/static/icon.png?t=" + ts;
    setFaviconHref("/static/icon.png?t=" + ts);
    msg.textContent = "已恢复默认图标";
  } catch (e) { msg.textContent = (e.message || "恢复失败"); }
});

async function previewTheme(name) {
  try {
    const d = await GET("/api/appearance?theme=" + encodeURIComponent(name));
    appearance.vars = d.vars; applyVars(d.vars);
  } catch (e) { }
}

// 选主题即时持久化：进/出外观页会触发 loadAppearance 重新 GET 后端并覆盖样式，
// 若不即时保存，未点「保存外观」就切走再回来会跳回旧主题。
async function saveTheme(name) {
  try { await POST("/api/appearance", { theme: name }); } catch (e) { }
}

function renderThemeGrid() {
  const grid = $("#themeGrid");
  if (!grid) return;
  grid.innerHTML = Object.entries(appearance.themes).map(([key, t]) => {
    const sel = key === appearance.theme ? " selected" : "";
    return `<div class="theme-swatch${sel}" data-theme="${esc(key)}" title="${esc(t.label)}">
      <span class="sw-dot" style="background:${t.accent || "#4f8cff"}"></span>
      <span class="sw-label">${esc(t.label)}</span>
    </div>`;
  }).join("");
  $$("#themeGrid .theme-swatch").forEach(el => el.addEventListener("click", () => {
    appearance.theme = el.dataset.theme;
    $$("#themeGrid .theme-swatch").forEach(x => x.classList.toggle("selected", x === el));
    previewTheme(el.dataset.theme).catch(() => { });
    saveTheme(el.dataset.theme);
  }));
}

function setWindowTitle(t) {
  try {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.set_title) {
      window.pywebview.api.set_title(t);
    }
  } catch (e) { }
}

$("#btnSaveAppearance").addEventListener("click", async () => {
  const btn = $("#btnSaveAppearance");
  const t = $("#titleInput").value.trim() || "肥鱼娘 · App 控制台";
  btn.disabled = true; btn.textContent = "保存中…";
  try {
    const r = await POST("/api/appearance", { theme: appearance.theme, title: t });
    appearance.title = r.title; appearance.vars = r.vars || appearance.vars;
    applyVars(appearance.vars);
    applyTitle(r.title);
    setWindowTitle(r.title);
    $("#titleInput").value = r.title;
    $("#titlePreview").textContent = r.title;
    toast("外观已保存");
  } catch (e) { toast(e.message, true); }
  btn.disabled = false; btn.textContent = "保存外观";
});

$("#btnResetTitle").addEventListener("click", () => {
  $("#titleInput").value = "肥鱼娘 · App 控制台";
  $("#titlePreview").textContent = "肥鱼娘 · App 控制台";
});

/* ---------------- 构建助手（对话式 Agent：像 Codex / WorkBuddy 那样边聊边改） ---------------- */
const bchat = {
  session: "",        // 当前会话 id
  sessions: [],       // 会话列表
  ctx: [],            // 选中的参考文件（相对路径）
  dir: "",            // 左栏当前浏览目录
  drafts: {},         // 未保存草稿（agent / plugin）
  sending: false,
  timer: null,
};

function _bcModel() {
  const val = $("#bcModel").value;
  let provider = "", model = null;
  if (val === "__custom__") model = $("#bcCustomModel").value.trim() || null;
  else if (val) provider = val;
  return { provider, model, think: $("#bcThink").value || "low" };
}

function _bcToggleCustomModel() {
  $("#bcCustomModel").classList.toggle("hidden", $("#bcModel").value !== "__custom__");
}

async function loadBuilder() {
  await _bcFillModels();
  await _bcLoadSessions();
  await _bcLoadSettings();
  _bcLoadDir();
  _bcRefreshHistory();
  _bcRenderCtx();
}

// ---- 设置：工作区位置 / 权限 / 高危确认 ----
async function _bcLoadSettings() {
  try {
    const d = await GET("/api/builder/settings");
    if (!d.ok) throw new Error(d.error || "读取失败");
    const s = d.settings || {};
    bchat.settings = s;
    $("#bcWs").value = s.workspace || "";
    $("#bcWsHint").textContent = "当前生效工作区：" + d.workspace
      + (d.is_custom ? "（自定义）" : "（应用目录）");
    const sel = $("#bcPerm");
    sel.innerHTML = "";
    (d.modes || []).forEach(m => {
      const o = document.createElement("option");
      o.value = m.id;                       // 值仍是英文 id（后端据此判定）
      o.textContent = m.label || m.id;      // 界面显示中文名
      o.dataset.desc = m.desc || "";
      sel.appendChild(o);
    });
    sel.value = s.permission_mode || "default";
    _bcPermHint();
    $("#bcConfirmOverwrite").checked = s.confirm_overwrite !== false;
    $("#bcConfirmSensitive").checked = s.confirm_sensitive !== false;
    $("#bcConfirmInstall").checked = s.confirm_install !== false;
    $("#bcAutoBackup").checked = s.auto_backup !== false;
    $("#bcRemember").checked = s.remember_approvals !== false;
    $("#bcWeb").checked = s.web_enabled !== false;
    $("#bcWebChars").value = s.web_max_chars || 20000;
    $("#bcWebPages").value = s.web_max_pages || 5;
    $("#bcMaxSteps").value = s.max_steps || 14;
    $("#bcSettingsMsg").textContent = "";
    bchat.mode = s.permission_mode || "default";
    _bcRenderRules(d.rules || []);
  } catch (e) { $("#bcWsHint").textContent = "读取设置失败: " + e.message; }
  _bcRefreshApprovals();
}

// ---- 已记住的批准（减少重复询问） ----
function _bcRenderRules(rules) {
  const box = $("#bcRules");
  box.innerHTML = "";
  if (!rules.length) {
    box.innerHTML = '<div class="hint wrap">暂无。批准待确认操作时可选择「记住」，之后同类操作不再询问。</div>';
    return;
  }
  rules.forEach(r => {
    const row = document.createElement("div");
    row.className = "bc-rule";
    const txt = document.createElement("span");
    txt.className = "bc-rule-desc";
    txt.textContent = r.desc || r.tool;
    row.appendChild(txt);
    const del = document.createElement("b");
    del.textContent = "✕";
    del.title = "删除这条记忆";
    del.addEventListener("click", async () => {
      try {
        await POST("/api/builder/rules/delete", { id: r.id });
        _bcLoadSettings();
      } catch (e) { toast(e.message, true); }
    });
    row.appendChild(del);
    box.appendChild(row);
  });
}

function _bcPermHint() {
  const sel = $("#bcPerm");
  const opt = sel.options[sel.selectedIndex];
  $("#bcPermHint").textContent = opt && opt.dataset.desc ? opt.dataset.desc : "";
}

async function _bcSaveSettings(patch, msgEl) {
  const el = msgEl || $("#bcSettingsMsg");
  el.textContent = "保存中…";
  try {
    const r = await POST("/api/builder/settings/save", patch);
    if (!r.ok) throw new Error(r.error || "保存失败");
    el.textContent = "已保存（" + (r.changed || []).join("、") + "）";
    await _bcLoadSettings();
    // 工作区变了：旧相对路径在新工作区里可能不存在，文件树回到根
    _bcLoadDir((r.changed || []).includes("workspace") ? "" : bchat.dir);
  } catch (e) { el.textContent = e.message; toast(e.message, true); }
}

// ---- 待确认操作（高危） ----
async function _bcRefreshApprovals() {
  try {
    const d = await GET("/api/builder/approvals");
    _bcRenderApprovals(d.pending || []);
  } catch (e) { /* 静默 */ }
}

function _bcRenderApprovals(list) {
  const box = $("#bcApprovals");
  box.innerHTML = "";
  if (!list.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const head = document.createElement("div");
  head.className = "bc-ap-head";
  const headTxt = document.createElement("span");
  headTxt.textContent = "待确认操作 " + list.length + " 项 —— 批准后才会执行";
  head.appendChild(headTxt);
  if (list.length > 1) {
    const all = document.createElement("button");
    all.className = "btn ghost sm";
    all.textContent = "全部批准";
    all.style.marginLeft = "8px";
    all.addEventListener("click", () => _bcApproveAll(all));
    head.appendChild(all);
  }
  box.appendChild(head);
  list.forEach(it => {
    const card = document.createElement("div");
    card.className = "bc-ap";
    const info = document.createElement("div");
    info.className = "bc-ap-info";
    const risk = document.createElement("span");
    risk.className = "bc-ap-risk " + (it.level === "high" ? "high" : "low");
    risk.textContent = it.level === "high" ? "高危" : "普通";
    info.appendChild(risk);
    const reason = document.createElement("span");
    reason.className = "bc-ap-reason";
    reason.textContent = (it.tool || "") + " · " + (it.reason || "");
    info.appendChild(reason);
    card.appendChild(info);
    if (it.tool === "write_file" && it.args && it.args.content) {
      const c = String(it.args.content);
      const pre = document.createElement("pre");
      pre.className = "bc-json";
      pre.textContent = "内容预览（" + c.length + " 字符）：\n" + c.slice(0, 700) + (c.length > 700 ? "\n…（已截断）" : "");
      card.appendChild(pre);
    }
    if (it.tool === "save_plugin" && it.args && it.args.name) {
      const pre = document.createElement("pre");
      pre.className = "bc-json";
      pre.textContent = "插件包：" + it.args.name;
      card.appendChild(pre);
    }
    const acts = document.createElement("div");
    acts.className = "row-actions";
    const scope = document.createElement("select");
    scope.className = "select";
    scope.style.maxWidth = "190px";
    const isWrite = it.tool === "write_file";
    const dir = isWrite ? (it.args.path || "").replace(/\/[^/]*$/, "") : "";
    const opts = [["once", "仅本次"], ["file", isWrite ? "记住此文件" : "记住这个"]];
    if (isWrite && dir) opts.push(["dir", "记住此目录"]);
    opts.push(["all", "记住全部同类"]);
    opts.forEach(o => {
      const el = document.createElement("option");
      el.value = o[0]; el.textContent = o[1];
      scope.appendChild(el);
    });
    // 完全访问档位默认「记住此文件」，以减少重复询问
    scope.value = (bchat.mode === "full" && opts.length > 1) ? "file" : "once";
    const ok = document.createElement("button");
    ok.className = "btn primary sm"; ok.textContent = "批准执行";
    const no = document.createElement("button");
    no.className = "btn ghost sm"; no.textContent = "拒绝";
    const st = document.createElement("span");
    st.className = "hint";
    ok.addEventListener("click", () => _bcDecide(it.id, true, st, ok, no, scope.value));
    no.addEventListener("click", () => _bcDecide(it.id, false, st, ok, no, "once"));
    acts.append(scope, ok, no, st);
    card.appendChild(acts);
    box.appendChild(card);
  });
}

async function _bcDecide(id, approve, st, btnA, btnB, scope) {
  btnA.disabled = true; btnB.disabled = true;
  st.textContent = approve ? "执行中…" : "处理中…";
  try {
    const body = approve
      ? { id, remember: !!(scope && scope !== "once"), scope: scope || "once" }
      : { id };
    const r = await POST(approve ? "/api/builder/approval/approve" : "/api/builder/approval/reject", body);
    if (!r.ok) throw new Error(r.error || "处理失败");
    st.textContent = r.rule && r.rule.desc ? ("已执行，并记住：" + r.rule.desc) : (approve ? "已执行" : "已拒绝");
    toast(r.rule && r.rule.desc ? ("已批准执行，后续同类操作不再询问") : (approve ? "已批准并执行" : "已拒绝该操作"));
    if (approve && r.result && r.result.path) {
      _bcLoadDir(bchat.dir);
      _bcOpenFile(r.result.path);
    }
    _bcRefreshApprovals();
    _bcRefreshHistory();
    if (r.rule) _bcLoadSettings();
  } catch (e) {
    st.textContent = e.message;
    toast(e.message, true);
    btnA.disabled = false; btnB.disabled = false;
  }
}

async function _bcApproveAll(btn) {
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "执行中…";
  try {
    const r = await POST("/api/builder/approval/approve_all", {});
    toast("已批准 " + r.approved + " / " + r.total + " 项");
    _bcRefreshApprovals();
    _bcRefreshHistory();
    _bcLoadDir(bchat.dir);
  } catch (e) { toast(e.message, true); }
  finally { btn.textContent = prev; btn.disabled = false; }
}

async function _bcFillModels() {
  try {
    const d = await GET("/api/providers");
    const sel = $("#bcModel");
    sel.innerHTML = '<option value="">系统默认模型</option>';
    (d.models || []).forEach(m => {
      const o = document.createElement("option");
      o.value = m.name;
      // 供应商名与模型名相同时不重复显示（如 deepseek · deepseek-flash → deepseek-flash）
      o.textContent = (m.model && m.model !== m.name)
        ? (m.name + " · " + m.model) : (m.model || m.name);
      o.title = o.textContent;
      sel.appendChild(o);
    });
    const custom = document.createElement("option");
    custom.value = "__custom__"; custom.textContent = "✎ 自定义…";
    sel.appendChild(custom);
  } catch (e) { /* 保留静态选项 */ }
  const saved = localStorage.getItem("bc_model");
  const sel = $("#bcModel");
  if (saved && sel.querySelector('option[value="' + (window.CSS && CSS.escape ? CSS.escape(saved) : saved) + '"]')) {
    sel.value = saved;
  } else if (saved) {
    sel.value = "__custom__"; $("#bcCustomModel").value = saved;
  }
  $("#bcThink").value = localStorage.getItem("bc_think") || "low";
  _bcToggleCustomModel();
}

// ---- 会话 ----
async function _bcLoadSessions() {
  try {
    const d = await GET("/api/builder/sessions");
    bchat.sessions = d.sessions || [];
  } catch (e) { bchat.sessions = []; }
  const sel = $("#bcSession");
  sel.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = ""; blank.textContent = "（新会话）";
  sel.appendChild(blank);
  bchat.sessions.forEach(s => {
    const o = document.createElement("option");
    o.value = s.id;
    o.textContent = (s.title || s.id) + "  (" + s.count + ")";
    sel.appendChild(o);
  });
  if (!bchat.session) {
    const saved = localStorage.getItem("bc_session");
    bchat.session = bchat.sessions.some(s => s.id === saved) ? saved
      : (bchat.sessions[0] ? bchat.sessions[0].id : "");
  }
  sel.value = bchat.session || "";
}

async function _bcOpenSession(sid) {
  bchat.session = sid || "";
  localStorage.setItem("bc_session", bchat.session);
  const chat = $("#bcChat");
  chat.innerHTML = "";
  if (!bchat.session) {
    _bcHint("新会话：直接描述你要构建 / 修改的内容，我会自己看代码、改文件并查语法。");
    _bcRenderCtx();
    return;
  }
  try {
    const d = await GET("/api/builder/session?name=" + encodeURIComponent(bchat.session));
    const st = d.state || {};
    bchat.drafts = st.drafts || {};
    _bcRenderMessages(st.messages || []);
    Object.values(bchat.drafts).forEach(dr => dr && _bcRenderDraft(dr));
  } catch (e) { toast("会话加载失败: " + e.message, true); }
}

async function _bcNewSession() {
  try {
    const r = await POST("/api/builder/session/new", {});
    if (!r.ok) throw new Error(r.error || "失败");
    bchat.session = r.session;
    await _bcLoadSessions();
    await _bcOpenSession(r.session);
    toast("已新建会话");
  } catch (e) { toast(e.message, true); }
}

async function _bcDelSession() {
  if (!bchat.session) return toast("当前没有会话", true);
  if (!confirm("删除会话 " + bchat.session + " ？")) return;
  try {
    await POST("/api/builder/session/delete", { name: bchat.session });
    bchat.session = "";
    await _bcLoadSessions();
    await _bcOpenSession(bchat.session);
  } catch (e) { toast(e.message, true); }
}

function _bcHint(text) {
  const chat = $("#bcChat");
  const d = document.createElement("div");
  d.className = "bc-hint";
  d.textContent = text;
  chat.appendChild(d);
}

// ---- 对话渲染 ----
function _bcMsgEl(role, text) {
  const chat = $("#bcChat");
  const wrap = document.createElement("div");
  wrap.className = "bc-msg bc-" + role;
  const head = document.createElement("div");
  head.className = "bc-msg-role";
  head.textContent = role === "user" ? "你" : "构建助手";
  const body = document.createElement("div");
  body.className = "bc-msg-text";
  body.textContent = text || "";
  wrap.appendChild(head); wrap.appendChild(body);
  chat.appendChild(wrap);
  return wrap;
}

function _bcUserMsg(text) { _bcMsgEl("user", text); }

// ---- 思维链卡片：思考中展开、结束后折叠，点箭头展开 ----
function _bcThinkCard(text, opts) {
  const o = opts || {};
  const chat = $("#bcChat");
  const card = document.createElement("details");
  card.className = "bc-think" + (o.live ? " live" : "");
  if (o.live) card.open = true;          // 思考中：展开显示
  const sum = document.createElement("summary");
  const label = document.createElement("span");
  label.className = "bc-think-label";
  const meta = document.createElement("span");
  meta.className = "bc-think-meta";
  const body = document.createElement("pre");
  body.className = "bc-think-body";
  sum.appendChild(label);
  sum.appendChild(meta);
  card.appendChild(sum);
  card.appendChild(body);
  chat.appendChild(card);
  const api = {
    el: card,
    setLive(t) { label.textContent = "💭 思考中…"; meta.textContent = t || ""; },
    append(t) {                          // 流式增量：边想边显示
      if (!t) return;
      body.textContent += t;
      meta.textContent = body.textContent.length + " 字（流式）";
      if (card.open) body.scrollTop = body.scrollHeight;
    },
    setText(t) {
      body.textContent = t || "";
      const n = (t || "").length;
      card.classList.remove("live");
      card.open = false;                 // 本轮思考结束 → 自动折叠
      label.textContent = "💭 思考过程";
      meta.textContent = n + " 字 · 点此展开";
    },
    finish(count) {
      card.classList.remove("live");
      card.open = false;
      const t = body.textContent || "";
      label.textContent = "💭 思考过程";
      meta.textContent = (count ? count + " 轮 · " : "") + t.length + " 字 · 点此展开";
    },
  };
  if (!o.live) api.setText(text);
  return api;
}

function _bcRenderMessages(msgs) {
  const chat = $("#bcChat");
  chat.innerHTML = "";
  let shown = 0;
  (msgs || []).forEach(m => {
    const role = m.role;
    if (role === "user") { _bcMsgEl("user", m.content || ""); shown++; }
    else if (role === "assistant") {
      if (m._reasoning) { _bcThinkCard(m._reasoning, {}); shown++; }
      if (m.content) { _bcMsgEl("assistant", m.content); shown++; }
      (m.tool_calls || []).forEach(tc => {
        let args = {};
        try { args = JSON.parse((tc.function || {}).arguments || "{}"); } catch (e) { }
        _bcToolCard({ tool: (tc.function || {}).name, args, ok: null, result: {} });
      });
    }
  });
  if (!shown) _bcHint("这个会话还没有内容。直接描述你要做的事即可。");
  _bcScroll();
}

function _bcScroll() {
  const chat = $("#bcChat");
  chat.scrollTop = chat.scrollHeight;
}

function _bcToolCard(step) {
  const chat = $("#bcChat");
  const res0 = step.result || {};
  const isPending = !!res0.pending;
  const auto = res0.auto_approved;
  const card = document.createElement("details");
  card.className = "bc-tool" + (isPending ? " pending" : (step.ok === false ? " bad" : ""));
  const sum = document.createElement("summary");
  const icon = isPending ? "⏳" : (step.ok === false ? "✗" : (step.ok === null ? "•" : "✓"));
  const tail = isPending
    ? "待用户确认：" + (res0.reason || "")
    : (auto ? "已记住的批准，自动执行：" + auto : _bcArgsBrief(step.args));
  sum.innerHTML = '<span class="bc-tool-name">' + icon + " " + (step.tool || "tool") + "</span>"
    + '<span class="bc-tool-args">' + tail + "</span>";
  card.appendChild(sum);
  const box = document.createElement("div");
  box.className = "bc-tool-body";
  const res = step.result || {};
  if (res.diff && res.diff.length) {
    const pre = document.createElement("pre");
    pre.className = "bc-diff";
    pre.textContent = res.diff.join("\n");
    box.appendChild(pre);
  }
  const pre2 = document.createElement("pre");
  pre2.className = "bc-json";
  pre2.textContent = _bcBrief(step.result);
  box.appendChild(pre2);
  card.appendChild(box);
  chat.appendChild(card);
  _bcScroll();
}

function _bcArgsBrief(args) {
  if (!args) return "";
  const keys = Object.keys(args);
  if (!keys.length) return "";
  const kv = keys.slice(0, 3).map(k => {
    let v = args[k];
    if (typeof v === "string") v = v.length > 60 ? v.slice(0, 60) + "…" : v;
    else v = JSON.stringify(v);
    return k + "=" + v;
  });
  return kv.join("  ");
}

function _bcBrief(obj) {
  try {
    const s = JSON.stringify(obj, null, 1);
    if (s === undefined) return "";
    return s.length > 2600 ? s.slice(0, 2600) + "\n…（已截断）" : s;
  } catch (e) { return String(obj); }
}

// ---- 流式对话：读 SSE（chunked），实时渲染思维链与正文 ----
async function _bcChatStream(payload, think) {
  const resp = await fetch("/api/builder/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.assign({}, payload, { stream: true })),
  });
  if (!resp.ok || !resp.body) {
    throw new Error("流式接口不可用（HTTP " + resp.status + "）");
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let done = null;
  let renderedTools = 0;
  let replyEl = null;
  let replyText = "";
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buf += dec.decode(chunk.value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const frame = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 2);
      if (!frame.startsWith("data:")) continue;
      let ev;
      try { ev = JSON.parse(frame.slice(5).trim()); } catch (e) { continue; }
      if (ev.type === "delta") {
        if (ev.kind === "think") think.append(ev.text);
        else if (ev.kind === "content") {
          if (!replyEl) replyEl = _bcMsgEl("assistant", "");
          replyText += ev.text;
          const body = replyEl.querySelector(".bc-msg-text");
          if (body) body.textContent = replyText;
          _bcScroll();
        }
      } else if (ev.type === "think_end") {
        think.setText(ev.text || "");
      } else if (ev.type === "tool_start") {
        think.setLive("正在调用 " + (ev.tool || "工具") + "…");
      } else if (ev.type === "tool_end") {
        _bcToolCard(ev.step || {});
        renderedTools++;
      } else if (ev.type === "notice") {
        toast(ev.text || "提示", true);
      } else if (ev.type === "done") {
        done = ev.payload || {};
      } else if (ev.type === "error") {
        throw new Error(ev.error || "流式对话失败");
      }
    }
  }
  if (!done) {
    const err = new Error("流式响应中断（未收到完成事件）");
    // 已经渲染过增量内容时不做非流式降级，避免界面出现重复内容
    err.__partial = (renderedTools > 0 || !!replyEl || (think.el.querySelector(".bc-think-body") || {}).textContent);
    throw err;
  }
  done.__renderedTools = renderedTools;
  done.__replyEl = replyEl;
  return done;
}

// ---- 发送 ----
async function _bcSend() {
  if (bchat.sending) return;
  const input = $("#bcInput");
  const msg = input.value.trim();
  if (!msg) return toast("先描述你要做的事", true);
  const msel = _bcModel();
  localStorage.setItem("bc_model", msel.provider || msel.model || "");
  localStorage.setItem("bc_think", msel.think);

  input.value = "";
  _bcUserMsg(msg);
  // 思考中的占位（展开状态），结束时被真实思维链替换并自动折叠
  const think = _bcThinkCard("", { live: true });
  think.setLive("0s");
  bchat.sending = true;
  $("#bcSend").disabled = true;
  $("#bcStop").style.display = "inline-block";
  const t0 = Date.now();
  bchat.timer = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    $("#bcStatus").textContent = "进行中 " + s + "s …";
    think.setLive(s + "s（" + (msel.think === "low" ? "快速" : msel.think === "high" ? "深度" : "标准") + "）");
  }, 1000);
  const useStream = $("#bcStream").checked;
  try {
    const payload = {
      session: bchat.session, message: msg,
      model: msel.model, think: msel.think, provider: msel.provider,
      context_paths: bchat.ctx, use_history: $("#bcUseHistory").checked,
    };
    let r, streamed = false;
    if (useStream) {
      try {
        r = await _bcChatStream(payload, think);
        streamed = true;
      } catch (se) {
        if (se.__partial) throw se;          // 已渲染部分增量：直接报错，不重复渲染
        // 流式端点完全没产出内容时退化为普通请求，保证对话可用
        toast("流式失败，已降级为普通请求：" + se.message, true);
        r = await POST("/api/builder/chat", payload);
      }
    } else {
      r = await POST("/api/builder/chat", payload);
    }
    // 用真实内容替换占位：有思维链就展示（折叠），没有则给出提示
    const thinks = (r.thinkings || []).filter(t => t && t.text);
    if (thinks.length) {
      if (!streamed) think.setText(thinks.map(t => t.text).join("\n\n———\n\n"));
      think.finish(thinks.length);
    } else {
      think.el.remove();
      const tip = document.createElement("div");
      tip.className = "hint wrap bc-think-tip";
      tip.textContent = "本轮未返回思考内容：可能是本轮问题较简单、模型未产生推理，"
        + "或该模型/供应商不支持思维链。可在下方把「思考」切到标准或深度，"
        + "或改用支持推理的模型（如 deepseek-reasoner）后重试。";
      $("#bcChat").appendChild(tip);
    }
    if (streamed) {
      // 工具卡片与正文已在流中实时渲染，只补上没收到的部分
      const all = (r.blocks || []).filter(b => b && b.type !== "think");
      all.slice(r.__renderedTools || 0).forEach(_bcToolCard);
      if (r.__replyEl) {
        // 已有流式正文气泡：用最终文本校正一次
        const body = r.__replyEl.querySelector(".bc-msg-text");
        if (body && r.reply && body.textContent !== r.reply) body.textContent = r.reply;
        if (!r.reply) r.__replyEl.remove();
      } else if (r.reply) {
        _bcMsgEl("assistant", r.reply);
      }
    } else {
      // 按顺序渲染本轮块：思维链（已渲染）→ 工具卡片
      (r.blocks && r.blocks.length ? r.blocks : (r.steps || [])).forEach(b => {
        if (b && b.type === "think") return;      // 思维链已合并渲染
        _bcToolCard(b);
      });
      if (r.reply) _bcMsgEl("assistant", r.reply);
    }
    if (!r.ok) throw new Error(r.error || "对话失败");
    bchat.session = r.session || bchat.session;
    bchat.drafts = r.drafts || bchat.drafts;
    Object.values(bchat.drafts).forEach(d => d && _bcRenderDraft(d));
    $("#bcStatus").textContent = "完成（" + Math.round((Date.now() - t0) / 1000) + "s，"
      + (r.steps || []).length + " 个工具调用"
      + (thinks.length ? "，思考 " + thinks.length + " 段" : "")
      + (streamed ? "，流式" : "") + "）";
    _bcLoadSessions();
    _bcRefreshHistory();
    _bcRefreshApprovals();
  } catch (e) {
    think.el.remove();
    _bcMsgEl("assistant", "出错了：" + e.message);
    toast(e.message, true);
    $("#bcStatus").textContent = "失败";
  } finally {
    clearInterval(bchat.timer);
    bchat.sending = false;
    $("#bcSend").disabled = false;
    $("#bcStop").style.display = "none";
    _bcScroll();
  }
}

// ---- 草稿卡片（生成 / 改进产物，确认后落盘） ----
function _bcRenderDraft(draft) {
  const chat = $("#bcChat");
  const isPlugin = !!(draft.code && draft.name);
  const card = document.createElement("div");
  card.className = "bc-draft";
  const head = document.createElement("div");
  head.className = "bc-draft-head";
  head.textContent = (isPlugin ? "插件草稿：plugins/" + draft.name + "/" : "智能体草稿：" + (draft.name || ""))
    + "（未落盘）";
  card.appendChild(head);
  const ta = document.createElement("textarea");
  ta.className = "textarea code";
  ta.rows = 12;
  ta.spellcheck = false;
  ta.value = JSON.stringify(draft, null, 2);
  card.appendChild(ta);
  const acts = document.createElement("div");
  acts.className = "row-actions";
  const save = document.createElement("button");
  save.className = "btn primary sm"; save.textContent = "保存到系统";
  save.addEventListener("click", () => _bcSaveDraft(isPlugin, ta.value, save));
  acts.appendChild(save);
  const copy = document.createElement("button");
  copy.className = "btn ghost sm"; copy.textContent = "复制";
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(ta.value); toast("已复制"); }
    catch (e) { toast("复制失败", true); }
  });
  acts.appendChild(copy);
  const msg = document.createElement("span");
  msg.className = "hint";
  acts.appendChild(msg);
  card.appendChild(acts);
  chat.appendChild(card);
  _bcScroll();
}

async function _bcSaveDraft(isPlugin, text, btn) {
  btn.disabled = true;
  const msg = btn.parentElement.querySelector(".hint");
  msg.textContent = "保存中…";
  try {
    let data;
    try { data = JSON.parse(text); } catch (e) { throw new Error("JSON 不合法: " + e.message); }
    let r;
    if (isPlugin) {
      r = await POST("/api/builder/plugin/save", { name: data.name, manifest: data, code: data.code });
    } else {
      r = await POST("/api/builder/agent/save", { data });
    }
    if (!r.ok) throw new Error(r.error || "保存失败");
    msg.textContent = isPlugin ? ("已写入插件包 " + r.name) : ("已保存智能体 " + r.id);
    toast(isPlugin ? "插件已保存（可在插件页重新扫描）" : "智能体已保存");
    bchat.drafts = {};
  } catch (e) {
    msg.textContent = e.message;
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

// ---- 左侧：工作区文件树 ----
async function _bcLoadDir(dir) {
  bchat.dir = (dir || "").replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  $("#bcDirPath").textContent = bchat.dir || "/（根）";
  const box = $("#bcTree");
  box.textContent = "加载中…";
  try {
    const d = await POST("/api/builder/workspace/list", { dir: bchat.dir });
    if (!d.ok) { box.textContent = "读取失败: " + d.error; return; }
    _bcRenderTree(d.items || []);
  } catch (e) { box.textContent = "读取失败: " + e.message; }
}

function _bcRenderTree(items) {
  const box = $("#bcTree");
  box.innerHTML = "";
  items.sort((a, b) => (b.is_dir - a.is_dir) || a.name.localeCompare(b.name));
  items.forEach(it => {
    const row = document.createElement("div");
    row.className = "bc-row" + (it.is_dir ? " dir" : "");
    if (!it.is_dir) row.classList.add("file");
    const nm = document.createElement("span");
    nm.className = "bc-row-name";
    nm.textContent = (it.is_dir ? "▸ " : "· ") + it.name;
    row.appendChild(nm);
    if (!it.is_dir) {
      const sz = document.createElement("span");
      sz.className = "bc-row-size";
      sz.textContent = _fmtSize(it.size || 0);
      row.appendChild(sz);
      const add = document.createElement("button");
      add.className = "bc-row-btn";
      add.textContent = "＋参考";
      add.title = "作为上下文加入本次对话";
      add.addEventListener("click", e => { e.stopPropagation(); _bcAddCtx(it.rel); });
      row.appendChild(add);
    }
    row.addEventListener("click", () => {
      if (it.is_dir) _bcLoadDir(it.rel);
      else _bcOpenFile(it.rel);
    });
    box.appendChild(row);
  });
  if (!items.length) box.textContent = "（空目录）";
}

function _bcUp() {
  const d = bchat.dir.split("/").filter(Boolean);
  d.pop();
  _bcLoadDir(d.join("/"));
}

// ---- 参考文件（上下文） ----
function _bcAddCtx(path) {
  if (!path) return;
  if (bchat.ctx.indexOf(path) < 0) {
    if (bchat.ctx.length >= 20) return toast("参考文件最多 20 个", true);
    bchat.ctx.push(path);
    _bcRenderCtx();
    toast("已加入参考：" + path);
  }
}

function _bcDelCtx(path) {
  bchat.ctx = bchat.ctx.filter(p => p !== path);
  _bcRenderCtx();
}

function _bcRenderCtx() {
  const bar = $("#bcCtxBar");
  bar.innerHTML = "";
  if (!bchat.ctx.length) {
    bar.innerHTML = '<span class="hint">参考文件：无（在左侧文件上点「＋参考」，我会读取它作为上下文）</span>';
    return;
  }
  const label = document.createElement("span");
  label.className = "hint";
  label.textContent = "参考文件（" + bchat.ctx.length + "）：";
  bar.appendChild(label);
  bchat.ctx.forEach(p => {
    const chip = document.createElement("span");
    chip.className = "bc-chip";
    chip.innerHTML = '<span></span>';
    chip.firstChild.textContent = p;
    const x = document.createElement("b");
    x.textContent = "✕";
    x.addEventListener("click", () => _bcDelCtx(p));
    chip.appendChild(x);
    bar.appendChild(chip);
  });
  const clr = document.createElement("button");
  clr.className = "bc-row-btn";
  clr.textContent = "清空";
  clr.addEventListener("click", () => { bchat.ctx = []; _bcRenderCtx(); });
  bar.appendChild(clr);
}

// ---- 右侧：文件编辑器 ----
async function _bcOpenFile(path) {
  const pane = $("#bcEditorPane");
  pane.classList.remove("hidden");
  $("#bcFile").value = path;
  $("#bcContent").value = "读取中…";
  $("#bcDiffOut").classList.add("hidden");
  try {
    const d = await POST("/api/builder/workspace/read", { path });
    if (!d.ok) { $("#bcContent").value = ""; $("#bcFileMsg").textContent = d.error; return; }
    $("#bcContent").value = d.content || "";
    $("#bcFileMsg").textContent = d.size + " bytes";
  } catch (e) { $("#bcFileMsg").textContent = e.message; }
}

async function _bcDiffFile() {
  const path = $("#bcFile").value.trim();
  if (!path) return;
  try {
    const d = await POST("/api/builder/workspace/diff", { path, content: $("#bcContent").value });
    const out = $("#bcDiffOut");
    if (!d.ok) { out.textContent = "失败: " + d.error; out.classList.remove("hidden"); return; }
    out.textContent = (d.diff || []).join("\n") + "\n\n（旧 " + d.old_size + " 行 → 新 " + d.new_size + " 行）";
    out.classList.remove("hidden");
  } catch (e) { $("#bcFileMsg").textContent = e.message; }
}

async function _bcSaveFile() {
  const path = $("#bcFile").value.trim();
  if (!path) return;
  if (!confirm("写入 " + path + " ？原文件会自动备份到 data/builder_bak/")) return;
  try {
    const d = await POST("/api/builder/workspace/write", { path, content: $("#bcContent").value, backup: true });
    $("#bcFileMsg").textContent = d.ok
      ? ("已保存 " + d.size + "B" + (d.backup ? "（已备份）" : ""))
      : ("失败: " + d.error);
    if (d.ok) toast("已保存 " + path);
  } catch (e) { $("#bcFileMsg").textContent = e.message; }
}

function _bcCloseEditor() { $("#bcEditorPane").classList.add("hidden"); }

// ---- 左栏页签 + 历史 ----
function _bcTab(name) {
  $$("#page-builder .bc-tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $("#bcPaneFiles").classList.toggle("hidden", name !== "files");
  $("#bcPaneHistory").classList.toggle("hidden", name !== "history");
  $("#bcPaneSettings").classList.toggle("hidden", name !== "settings");
  if (name === "history") _bcRefreshHistory();
  if (name === "settings") _bcLoadSettings();
}

async function _bcRefreshHistory() {
  const stat = $("#bcHistStat");
  stat.textContent = "加载中…";
  try {
    const d = await GET("/api/builder/history");
    if (!d.ok) throw new Error(d.error || "获取失败");
    _bcRenderHistory(d.history || []);
    stat.textContent = "共 " + (d.history || []).length + " 条";
  } catch (e) { stat.textContent = "失败: " + e.message; }
}

function _bcRenderHistory(list) {
  const box = $("#bcHistory");
  box.innerHTML = "";
  if (!list.length) {
    box.innerHTML = '<div class="hint wrap">暂无记录。生成 / 改进 / 改文件后会自动记录。</div>';
    return;
  }
  list.forEach(rec => {
    const item = document.createElement("div");
    item.className = "hist-item";
    const meta = document.createElement("div");
    meta.className = "hist-meta";
    const badge = rec.mode === "plugin" ? "插件" : (rec.mode === "agent" ? "智能体" : "对话");
    meta.innerHTML = '<span class="hist-mode">' + badge + "</span>"
      + (rec.ok ? '<span class="ok">✓</span>' : '<span class="bad">✗</span>')
      + ' <span class="hint">' + _fmtTime(rec.time) + " · " + (rec.event || "")
      + " · " + (rec.rounds || 1) + " 步</span>";
    item.appendChild(meta);
    const req = document.createElement("div");
    req.className = "hist-req";
    req.textContent = rec.requirement || "(无描述)";
    item.appendChild(req);
    const acts = document.createElement("div");
    acts.className = "row-actions";
    const again = document.createElement("button");
    again.className = "btn ghost sm";
    again.textContent = "继续改这个";
    again.addEventListener("click", () => {
      const key = rec.key || "";
      let t;
      if (rec.mode === "plugin" && key) t = "继续改进插件 " + key + "：";
      else if (rec.mode === "agent" && key) t = "继续改进智能体 " + key + "：";
      else t = "继续：" + (rec.requirement || "");
      $("#bcInput").value = t;
      $("#bcInput").focus();
    });
    acts.appendChild(again);
    if (rec.artifact && rec.mode !== "chat") {
      const load = document.createElement("button");
      load.className = "btn ghost sm";
      load.textContent = "载入草稿";
      load.addEventListener("click", () => _bcRenderDraft(rec.artifact));
      acts.appendChild(load);
    }
    item.appendChild(acts);
    box.appendChild(item);
  });
}

// ---- 事件绑定 ----
function initBuilderUI() {
  $("#bcSession").addEventListener("change", () => _bcOpenSession($("#bcSession").value));
  $("#bcNewSession").addEventListener("click", _bcNewSession);
  $("#bcDelSession").addEventListener("click", _bcDelSession);
  $("#bcModel").addEventListener("change", _bcToggleCustomModel);
  $("#bcSend").addEventListener("click", _bcSend);
  $("#bcStop").addEventListener("click", () => {
    // HTTP 请求无法中途取消，提示用户等待本轮结束
    $("#bcStatus").textContent = "本轮结束后生效（HTTP 调用不可中断）";
  });
  $("#bcRefreshTree").addEventListener("click", () => _bcLoadDir(bchat.dir));
  $("#bcUp").addEventListener("click", _bcUp);
  $("#bcRefreshHist").addEventListener("click", _bcRefreshHistory);
  // 对话框下方：访问权限（切换即保存）
  $("#bcPerm").addEventListener("change", async () => {
    const opt = $("#bcPerm").selectedOptions[0];
    const label = opt ? opt.textContent : $("#bcPerm").value;
    try {
      const r = await POST("/api/builder/settings/save", { permission_mode: $("#bcPerm").value });
      if (!r.ok) throw new Error(r.error || "保存失败");
      await _bcLoadSettings();
      toast("访问权限已切换为：" + label);
    } catch (e) { toast(e.message, true); }
  });
  $("#bcWsApply").addEventListener("click", () => _bcSaveSettings(
    { workspace: $("#bcWs").value.trim() }, $("#bcWsHint")));
  // 从硬盘选择工作区目录
  $("#bcWsBrowse").addEventListener("click", _bcBrowseWorkspace);
  $("#pickClose").addEventListener("click", _bcPickClose);
  $("#pickCancel").addEventListener("click", _bcPickClose);
  $("#pickChoose").addEventListener("click", () => {
    if (!pickState.path) return toast("请先进入一个目录", true);
    _bcPickClose();
    _bcApplyWorkspace(pickState.path);
  });
  $("#pickUp").addEventListener("click", () => {
    if (!pickState.path) return;
    const parts = pickState.path.replace(/[\\/]+$/, "").split(/[\\/]/);
    parts.pop();
    const parent = parts.join("\\");
    _bcPickLoad(parent.match(/^[A-Za-z]:$/) ? parent + "\\" : (parent || ""));
  });
  $("#pickGo").addEventListener("click", () => _bcPickLoad($("#pickPath").value.trim()));
  $("#pickPath").addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); _bcPickLoad($("#pickPath").value.trim()); }
  });
  $("#pickModal").addEventListener("click", e => {
    if (e.target && e.target.id === "pickModal") _bcPickClose();   // 点遮罩关闭
  });
  $("#bcWsReset").addEventListener("click", () => {
    $("#bcWs").value = "";
    _bcSaveSettings({ workspace: "" }, $("#bcWsHint"));
  });
  $("#bcSettingsSave").addEventListener("click", () => _bcSaveSettings({
    workspace: $("#bcWs").value.trim(),
    permission_mode: $("#bcPerm").value,
    confirm_overwrite: $("#bcConfirmOverwrite").checked,
    confirm_sensitive: $("#bcConfirmSensitive").checked,
    confirm_install: $("#bcConfirmInstall").checked,
    auto_backup: $("#bcAutoBackup").checked,
    remember_approvals: $("#bcRemember").checked,
    web_enabled: $("#bcWeb").checked,
    web_max_chars: parseInt($("#bcWebChars").value || "20000", 10),
    web_max_pages: parseInt($("#bcWebPages").value || "5", 10),
    max_steps: parseInt($("#bcMaxSteps").value || "14", 10),
  }, $("#bcSettingsMsg")));
  $("#bcRemember").addEventListener("change", () => _bcSaveSettings(
    { remember_approvals: $("#bcRemember").checked }, $("#bcSettingsMsg")));
  $("#bcWeb").addEventListener("change", () => _bcSaveSettings(
    { web_enabled: $("#bcWeb").checked }, $("#bcSettingsMsg")));
  $("#bcRulesClear").addEventListener("click", async () => {
    try {
      await POST("/api/builder/rules/clear", {});
      toast("已清空批准记忆");
      _bcLoadSettings();
    } catch (e) { toast(e.message, true); }
  });
  $$("#page-builder .bc-tab").forEach(b => b.addEventListener("click", () => _bcTab(b.dataset.tab)));
  $("#bcFileDiff").addEventListener("click", _bcDiffFile);
  $("#bcFileSave").addEventListener("click", _bcSaveFile);
  $("#bcEditorClose").addEventListener("click", _bcCloseEditor);
  $("#bcFileCtx").addEventListener("click", () => _bcAddCtx($("#bcFile").value.trim()));
  // 侧边栏收回 / 拉出（状态存 localStorage；收回后输入框仍贴在底端）
  $("#bcSideHide").addEventListener("click", () => _bcSide(true));
  $("#bcSideShow").addEventListener("click", () => _bcSide(false));
  _bcSide(localStorage.getItem("bc_side_collapsed") === "1");
  $("#bcStream").checked = localStorage.getItem("bc_stream") !== "0";
  $("#bcStream").addEventListener("change", () => {
    localStorage.setItem("bc_stream", $("#bcStream").checked ? "1" : "0");
    toast($("#bcStream").checked ? "已开启流式输出" : "已关闭流式输出（等整轮返回后一次性显示）");
  });
  $("#bcInput").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); _bcSend(); }
  });
  $("#bcChat").addEventListener("click", e => {
    const a = e.target.closest ? e.target.closest("a") : null;
    if (a) e.preventDefault();
  });
}

// ---- 工作区目录选择：原生系统对话框优先，兜底内置目录浏览器 ----
async function _bcApplyWorkspace(path) {
  $("#bcWs").value = path || "";
  try {
    const r = await POST("/api/builder/settings/save", { workspace: path || "" });
    if (!r.ok) throw new Error(r.error || "保存失败");
    await _bcLoadSettings();
    _bcLoadDir("");                     // 新工作区：文件树回到根
    toast("工作区已切换到：" + (path || "应用目录"));
  } catch (e) { toast(e.message, true); }
}

async function _bcBrowseWorkspace() {
  const cur = ($("#bcWs").value || "").trim();
  // 1) pywebview 模式：系统「选择文件夹」对话框
  try {
    const api = (window.pywebview && window.pywebview.api) || null;
    if (api && typeof api.pick_folder === "function") {
      const r = await api.pick_folder(cur || null);
      if (r && r.supported) {
        if (r.path) return _bcApplyWorkspace(r.path);
        return;                          // 用户点了取消：不再弹内置选择器
      }
    }
  } catch (e) { /* 落到内置选择器 */ }
  // 2) 其它模式（Edge/浏览器）：内置目录浏览器
  _bcPickOpen(cur);
}

let pickState = { path: "", lastOk: "" };

function _bcPickOpen(start) {
  $("#pickModal").classList.remove("hidden");
  pickState.path = "";
  pickState.lastOk = "";
  const p = (start || "").trim();
  _bcPickLoad(p && /[\\/]/.test(p) ? p : "");
}

function _bcPickClose() { $("#pickModal").classList.add("hidden"); }

async function _bcPickLoad(path) {
  const msg = $("#pickMsg");
  msg.textContent = "加载中…";
  try {
    const d = await GET("/api/fs/dirs?path=" + encodeURIComponent(path || ""));
    if (!d.ok) {
      msg.textContent = (d.error || "读取失败") + (pickState.lastOk ? "（仍显示上一个目录）" : "");
      if (!pickState.lastOk) { pickState.path = ""; _bcPickRender({ path: "", dirs: [], quick: [] }); }
      return;
    }
    pickState.path = d.path || "";
    pickState.lastOk = pickState.path;
    _bcPickRender(d);
    msg.textContent = pickState.path
      ? ("当前浏览：" + pickState.path)
      : "选择盘符或常用位置开始浏览";
  } catch (e) { msg.textContent = "读取失败：" + e.message; }
}

function _bcPickRender(d) {
  $("#pickPath").value = d.path || "";
  const q = $("#pickQuick");
  q.innerHTML = "";
  (d.quick || []).forEach(item => {
    const b = document.createElement("button");
    b.className = "btn ghost sm";
    b.textContent = item.name;
    b.title = item.path;
    b.addEventListener("click", () => _bcPickLoad(item.path));
    q.appendChild(b);
  });
  const box = $("#pickList");
  box.innerHTML = "";
  const dirs = d.dirs || [];
  if (!dirs.length) {
    box.innerHTML = '<div class="hint wrap" style="padding:8px">'
      + (d.path ? "该目录下没有子目录" : "没有可浏览的盘符") + "</div>";
    return;
  }
  dirs.forEach(it => {
    const row = document.createElement("div");
    row.className = "pick-row" + (osSame(it.path, d.current_workspace) ? " cur" : "");
    const nm = document.createElement("span");
    nm.className = "pick-name";
    nm.textContent = "📁 " + it.name;
    row.appendChild(nm);
    const go = document.createElement("span");
    go.className = "pick-go";
    go.textContent = osSame(it.path, d.current_workspace) ? "当前工作区 ✓" : "进入 ›";
    row.appendChild(go);
    row.addEventListener("click", () => _bcPickLoad(it.path));
    box.appendChild(row);
  });
}

function osSame(a, b) {
  if (!a || !b) return false;
  const n = p => String(p).replace(/[\\/]+$/, "").toLowerCase();
  return n(a) === n(b);
}

// 侧边栏收起：只隐藏左栏（含对话底端输入区）占满，输入框仍在底端
function _bcSide(collapsed) {
  const page = $("#page-builder");
  if (!page) return;
  page.classList.toggle("bc-side-collapsed", !!collapsed);
  localStorage.setItem("bc_side_collapsed", collapsed ? "1" : "0");
}

function _fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const p = n => (n < 10 ? "0" : "") + n;
  return (d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function _fmtSize(b) {
  if (b < 1024) return b + "B";
  if (b < 1048576) return (b / 1024).toFixed(1) + "KB";
  return (b / 1048576).toFixed(1) + "MB";
}

/* ---------------- 启动 ---------------- */
window.addEventListener("DOMContentLoaded", async () => {
  connectSSE();
  loadAppearance().catch(() => { });
  loadBots().catch(() => { });
  await refreshStatus();
  try {
    const recent = await GET("/api/recent");
    // 与 SSE 回放重叠，addEventOnce 去重（同一事件只渲染一次）
    (recent.events || []).slice().reverse().forEach(ev => {
      if (!addEventOnce(ev)) return;
      if (ev.type === "message" && ev.text) addChatMsg(ev.role, ev.text);
      appendEventLog(ev);
    });
  } catch (e) { }
  setInterval(() => refreshStatus().catch(() => { }), 15000);
  $("#cfgClose").addEventListener("click", closeCfgModal);
  $("#cfgClose2").addEventListener("click", closeCfgModal);
  $("#cfgSave").addEventListener("click", savePluginConfig);
  $("#bgDim").addEventListener("click", closeCfgModal);
  // ----- 构建助手（对话式 Agent） -----
  initBuilderUI();

  // ----- 本地 AI 依赖按需后装（配置页） -----
  initLocalDeps();

  // ===== 记忆：导入 / 导出 =====
  $("#btnExportMem").addEventListener("click", async () => {
    try {
      const data = await GET("/api/memory/export");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "feiyu_memory_" + new Date().toISOString().slice(0, 10) + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
      toast("已开始下载记忆备份");
    } catch (e) { toast(e.message || "导出失败", true); }
  });

  async function _memImportFile(file, mode) {
    const msg = $("#memImportMsg");
    try {
      const data = JSON.parse(await file.text());
      msg.textContent = "导入中…";
      const r = await PB("/api/memory/import", { data, mode: mode || "replace" });
      if (!r.ok) throw new Error(r.error || "导入失败");
      const parts = Object.entries(r.imported || {}).map(([k, v]) => `${k}=${v}`).join("，");
      msg.textContent = "导入完成：" + parts;
      if (typeof renderMemory === "function") renderMemory();
    } catch (e) { msg.textContent = e.message; }
  }
  $("#memImportFile").addEventListener("change", e => {
    if (e.target.files[0]) _memImportFile(e.target.files[0], $("#memImportMode").value);
    e.target.value = "";
  });

  async function _memImportChatlog(file) {
    const msg = $("#memImportMsg");
    try {
      const text = await file.text();
      msg.textContent = "解析中…";
      const r = await PB("/api/memory/import_chatlog", {
        text, uid: ($("#memChatlogUid").value || "app_owner").trim(),
        target: $("#memChatlogTarget").value });
      if (!r.ok) throw new Error(r.error || "失败");
      msg.textContent = `聊天记录已解析 ${r.parsed} 条，写入 ${r.added} 条（目标：${r.target}）`;
      if (typeof renderMemory === "function") renderMemory();
    } catch (e) { msg.textContent = e.message; }
  }
  $("#memChatlogFile").addEventListener("change", e => {
    if (e.target.files[0]) _memImportChatlog(e.target.files[0]);
    e.target.value = "";
  });

  // ===== 构建助手：导入智能体 + 接入外部 API =====
  $("#bldImportAgent").addEventListener("change", async e => {
    const f = e.target.files[0]; if (!f) return;
    const msg = $("#bldImportMsg");
    try {
      const data = JSON.parse(await f.text());
      msg.textContent = "导入中…";
      const r = await POST("/api/builder/agent/import", { agent: data });
      if (!r.ok) throw new Error(r.error || "导入失败");
      msg.textContent = "已导入智能体：" + (r.name || r.id);
      toast("智能体已导入");
    } catch (err) { msg.textContent = err.message; }
    e.target.value = "";
  });

  $("#btnConnectExt").addEventListener("click", async () => {
    const msg = $("#extMsg");
    const caps = Array.from($("#extCaps").selectedOptions).map(o => o.value);
    const payload = {
      name: $("#extName").value.trim(),
      base_url: $("#extBase").value.trim(),
      api_key: $("#extKey").value,
      model: $("#extModel").value.trim(),
      system_prompt: $("#extSys").value,
      capabilities: caps,
    };
    if (!payload.name || !payload.base_url || !payload.model) return toast("名称 / BaseURL / 模型 均为必填", true);
    try {
      msg.textContent = "接入中…";
      const r = await POST("/api/builder/agent/connect_external", payload);
      if (!r.ok) throw new Error(r.error || "接入失败");
      msg.textContent = "已接入外部智能体：" + r.name + "（供应商：" + r.provider + "）";
      toast("外部智能体已接入");
    } catch (err) { msg.textContent = err.message; }
  });

  // ===== 侧边栏 拉出 / 收回 =====
  (function () {
    const KEY = "sidebar-collapsed";
    const btn = $("#sidebarToggle");
    if (!btn) return;
    function apply(collapsed) {
      document.body.classList.toggle("sidebar-collapsed", collapsed);
      btn.textContent = collapsed ? "☰ 拉出" : "« 收回"; // 符号 + 文字
      btn.title = collapsed ? "展开侧边栏" : "收起侧边栏";
    }
    let collapsed = false;
    try { collapsed = localStorage.getItem(KEY) === "1"; } catch (e) { }
    apply(collapsed);
    btn.addEventListener("click", () => {
      collapsed = !collapsed;
      apply(collapsed);
      try { localStorage.setItem(KEY, collapsed ? "1" : "0"); } catch (e) { }
    });
  })();
});
