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
  const loaders = { dashboard: loadDashboard, chat: null, memory: loadMemory, summary: loadSummary, plugins: loadPlugins, config: loadConfig, appearance: loadAppearance };
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
  try {
    const ms = await GET("/api/memory/stats");
    $("#stProfiles").textContent = ms.profiles;
    $("#stNotes").textContent = ms.notes;
    $("#stKnowledge").textContent = ms.knowledge;
    $("#stReflection").textContent = ms.reflection;
  } catch (e) { }
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

async function sendChat() {
  const input = $("#chatInput");
  const text = input.value.trim();
  if (!text) return;
  if (state.chatBusy) { toast("她还在思考中，稍等一下…"); return; }
  input.value = "";
  // 乐观锁：立即置忙，不等 SSE status 回包（防快速双击导致重复提交/重复回复）
  state.chatBusy = true;
  const chip = $("#chatState");
  chip.textContent = "思考中…"; chip.classList.add("busy");
  addChatMsg("user", text, true);
  try {
    const r = await POST("/api/chat", { text, session: SESSION });
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
  const users = await GET("/api/memory/users");
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
    if (tab === "sessions") { const d = await GET("/api/memories"); body.innerHTML = renderSessions(d); return; }
    const d = await GET(map[tab] + (qs.toString() ? "?" + qs : ""));
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
    const r = await POST("/api/memory/action", { kind: "profiles", payload: { op: "save", uid, facts } });
    toast(r.ok ? "档案已保存" : (r.error || "保存失败"), !r.ok);
  }));
  $$("#memBody .act-del").forEach(b => b.addEventListener("click", async () => {
    const uid = b.closest(".mem-card").dataset.uid;
    if (!confirm(`确定删除 ${uid} 的人物档案？`)) return;
    await POST("/api/memory/action", { kind: "profiles", payload: { op: "delete", uid } });
    toast("已删除"); renderMemory();
  }));
  $$("#memBody .act-note-del").forEach(b => b.addEventListener("click", async () => {
    const li = b.closest("li");
    await POST("/api/memory/action", { kind: "notes", payload: { op: "delete", uid: li.dataset.uid, index: +li.dataset.idx } });
    renderMemory();
  }));
  $$("#memBody .act-note-add").forEach(b => b.addEventListener("click", async () => {
    const card = b.closest(".mem-card");
    const inp = $(".new-note", card);
    if (!inp.value.trim()) return;
    await POST("/api/memory/action", { kind: "notes", payload: { op: "add", uid: state.memUser || "app_owner", text: inp.value.trim() } });
    toast("已添加"); renderMemory();
  }));
  $$("#memBody [data-act='clear-persona']").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("清空该用户的人格记忆？")) return;
    await POST("/api/memory/action", { kind: "persona", payload: { op: "clear", uid: b.dataset.uid } });
    toast("已清空"); renderMemory();
  }));
  $$("#memBody [data-act='kb-del']").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("删除该知识主题？")) return;
    await POST("/api/memory/action", { kind: "knowledge", payload: { op: "delete", keyword: b.dataset.kw } });
    toast("已删除"); renderMemory();
  }));
  const kbAdd = $("#memBody .act-kb-add");
  if (kbAdd) kbAdd.addEventListener("click", async () => {
    const topic = $("#memBody .new-kw").value.trim();
    const facts = $("#memBody .new-facts").value.split(/[;；]/).map(s => s.trim()).filter(Boolean);
    if (!topic || !facts.length) return toast("主题与事实必填", true);
    const r = await POST("/api/memory/action", { kind: "knowledge", payload: { op: "add", topic, facts } });
    toast(r.ok ? "已入库" : (r.error || "失败"), !r.ok); renderMemory();
  });
}

/* ---------------- 总结中心 ---------------- */
async function loadSummary() {
  const ov = await GET("/api/summary/overview");
  const live = ov.live || {};
  $("#liveSummary").value = live.summary || "";
  $("#liveSummaryMeta").textContent = live.topic ? `当前话题: ${live.topic} · 短期记忆 ${live.short_turns} 条` : "";
  const tb = $("#tblSummaries tbody");
  tb.innerHTML = ov.items.map(i => `<tr><td>${esc(i.key)}</td><td>${i.turns}</td><td class="wrap">${esc(i.topic || "—")}</td><td class="wrap">${esc(i.summary || "—")}</td></tr>`).join("") || `<tr><td colspan="4" class="hint">暂无</td></tr>`;
}

$("#btnSaveSummary").addEventListener("click", async () => {
  const r = await POST("/api/summary/session", { text: $("#liveSummary").value });
  toast(r.ok ? (r.mode === "live" ? "已保存（运行中，立即生效）" : "已保存（落盘，下次启动核心生效）") : "保存失败", !r.ok);
});

$("#btnRunSummary").addEventListener("click", async () => {
  const btn = $("#btnRunSummary");
  btn.disabled = true; btn.textContent = "总结中…（LLM 调用约 10-30s）";
  const box = $("#sumResult"); box.classList.add("hidden");
  try {
    const r = await POST("/api/summary/run", {
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
}

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
  });
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
    appearance.themes = d.themes || {}; appearance.vars = d.vars || {};
    applyVars(appearance.vars);
    applyTitle(d.title);
    applyBg(appearance.bg);
    setBgPreview(appearance.bg);
    renderThemeGrid();
    $("#titleInput").value = d.title;
    $("#titlePreview").textContent = d.title;
  } catch (e) { /* 兜底用 CSS 默认主题 */ }
}

function applyBg(bg) {
  const body = document.body, dim = $("#bgDim");
  if (!bg) {
    body.style.backgroundImage = "";
    if (dim) dim.style.display = "none";
    return;
  }
  const url = bg === "local" ? "/api/appearance/bg" : bg;
  body.style.backgroundImage = `url("${url}")`;
  body.style.backgroundSize = "cover";
  body.style.backgroundPosition = "center";
  body.style.backgroundAttachment = "fixed";
  if (dim) dim.style.display = "block";
}

function setBgPreview(bg) {
  const el = $("#bgPreview");
  if (!el) return;
  if (!bg) { el.style.display = "none"; el.style.backgroundImage = ""; return; }
  el.style.display = "block";
  el.style.backgroundImage = `url("${bg === "local" ? "/api/appearance/bg" : bg}")`;
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
      appearance.bg = r.bg; applyBg(r.bg); setBgPreview(r.bg);
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
    appearance.bg = r.bg; applyBg(r.bg); setBgPreview(r.bg);
    msg.textContent = "背景已应用";
  } catch (e) { msg.textContent = (e.message || "应用失败"); }
});

$("#btnClearBg").addEventListener("click", async () => {
  const msg = $("#bgMsg");
  msg.textContent = "清除中…";
  try {
    const r = await POST("/api/appearance", { clear_bg: true });
    appearance.bg = r.bg; applyBg(r.bg); setBgPreview(r.bg);
    $("#bgUrl").value = "";
    msg.textContent = "已清除背景";
  } catch (e) { msg.textContent = (e.message || "清除失败"); }
});

async function previewTheme(name) {
  try {
    const d = await GET("/api/appearance?theme=" + encodeURIComponent(name));
    appearance.vars = d.vars; applyVars(d.vars);
  } catch (e) { }
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

/* ---------------- 启动 ---------------- */
window.addEventListener("DOMContentLoaded", async () => {
  connectSSE();
  loadAppearance().catch(() => { });
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
});
