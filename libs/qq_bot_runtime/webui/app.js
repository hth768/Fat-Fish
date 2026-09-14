// 肥鱼娘 · 猫娘计划 Web 控制台 —— 前端交互逻辑
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const SESSION = (() => {
  let s = localStorage.getItem('neko_session');
  if (!s) { s = (crypto.randomUUID ? crypto.randomUUID() : 's' + Date.now() + Math.random()); localStorage.setItem('neko_session', s); }
  return s;
})();

// ---------- 工具 ----------
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function badge(on, onTxt = '开启', offTxt = '关闭') {
  if (on === true || on === 'on' || on === 'True' || on === 1) return `<span class="badge on">${onTxt}</span>`;
  if (on === false || on === 'off' || on === 'False' || on === 0) return `<span class="badge off">${offTxt}</span>`;
  return `<span class="badge idle">${esc(on == null ? '—' : on)}</span>`;
}
async function api(path, opts = {}) {
  const r = await fetch('/' + path, opts);
  try { return await r.json(); } catch (e) { return { error: 'json 解析失败: ' + r.status }; }
}
function toast(msg, ms = 2600) {
  let t = $('.toast');
  if (!t) { t = document.createElement('div'); t.className = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add('show');
  clearTimeout(t._tm); t._tm = setTimeout(() => t.classList.remove('show'), ms);
}
function pretty(o) { try { return JSON.stringify(o, null, 2); } catch (e) { return String(o); } }

// ---------- 聊天状态 ----------
let chatStore = [];
let typing = false;
let es = null;

function connect() {
  if (es) return;
  es = new EventSource('/api/events?session=' + SESSION);
  es.onopen = () => setConn('on', '已连接');
  es.onmessage = (e) => {
    let ev; try { ev = JSON.parse(e.data); } catch (_) { return; }
    handleEvent(ev);
  };
  es.onerror = () => setConn('off', '连接断开（重试中）');
}
function setConn(state, text) {
  const d = $('#connDot'); if (d) d.className = 'status-dot ' + (state || '');
  const t = $('#connText'); if (t) t.textContent = text;
}
function handleEvent(ev) {
  switch (ev.type) {
    case 'message':
      chatStore.push({ role: 'assistant', text: ev.text, proactive: ev.proactive });
      renderChatLog(); break;
    case 'image':
      chatStore.push({ role: 'assistant', image: ev.url }); renderChatLog(); break;
    case 'audio':
      chatStore.push({ role: 'assistant', audio: ev.url }); renderChatLog(); break;
    case 'tts':
      if ('speechSynthesis' in window && ev.text) {
        try { const u = new SpeechSynthesisUtterance(ev.text); u.lang = 'zh-CN'; speechSynthesis.cancel(); speechSynthesis.speak(u); } catch (_) {}
      }
      break;
    case 'status':
      typing = (ev.state === 'thinking');
      renderChatLog(); break;
    case 'error':
      toast('错误：' + ev.text); break;
  }
}

async function sendChat(text) {
  text = (text || '').trim();
  if (!text) return;
  chatStore.push({ role: 'me', text });
  renderChatLog();
  const r = await api('api/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, session: SESSION })
  });
  if (!r.ok) toast(r.error || '发送失败（核心未启动？请用 python web_plugin.py 或 main.py --web）');
}
async function runCmd(text) {
  chatStore.push({ role: 'me', text });
  renderChatLog();
  const r = await api('api/chat', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, session: SESSION })
  });
  if (!r.ok) toast(r.error || '命令发送失败');
  else toast('已发送：' + text);
}

// ---------- 视图路由 ----------
const TITLES = {
  chat: '对话', brains: '大脑状态', memory: '记忆', knowledge: '知识库', emotion: '情绪',
  identity: '身份绑定', mc: '我的世界 (Minecraft)', pc: '电脑操控', pvz: '植物大战僵尸',
  bili: 'B 站直播', voice: '语音 / TTS', vision: '视觉感知',   plugins: '插件 / 服务',
  memory_browser: '记忆浏览',
  memory_browser: '记忆浏览',
  config: '配置', system: '系统 / 日志'
};
const AUTO_REFRESH = { brains: 5000, mc: 5000, pvz: 8000, system: 6000, bili: 8000, identity: 0, emotion: 0, plugins: 5000, memory_browser: 15000 };
let refreshTimer = null;

function switchView(v) {
  $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  $('#viewTitle').textContent = TITLES[v] || v;
  const c = $('#content');
  c.innerHTML = '<div class="empty">加载中…</div>';
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }

  const fn = VIEWS[v] || (() => { c.innerHTML = '<div class="empty">未实现</div>'; });
  fn(c);
  if (AUTO_REFRESH[v]) refreshTimer = setInterval(() => fn(c), AUTO_REFRESH[v]);
}

// ---------- 各视图 ----------
const VIEWS = {
  // 对话
  chat(c) {
    c.innerHTML = `
      <div class="chat-wrap">
        <div class="card" style="margin-bottom:0;flex:1;display:flex;flex-direction:column;min-height:0">
          <div class="chips" style="margin-bottom:10px">
            ${['/帮助','/mc自动','/电脑做','/pvz玩','/直播 开播','/记忆','/心情','/状态'].map(k => `<button class="chip" data-cmd="${esc(k)}">${esc(k)}</button>`).join('')}
          </div>
          <div class="chat-log" id="chatLog"></div>
          <div class="chat-input">
            <textarea id="chatInput" placeholder="和肥鱼娘说点什么…（Enter 发送，Shift+Enter 换行）"></textarea>
            <button class="btn" id="chatSend">发送</button>
          </div>
        </div>
      </div>`;
    $$('.chip', c).forEach(b => b.onclick = () => runCmd(b.dataset.cmd));
    const ta = $('#chatInput');
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(ta.value); }
    });
    $('#chatSend').onclick = () => sendChat(ta.value);
    renderChatLog();
  },

  // 大脑状态
  async brains(c) {
    const [st, br] = await Promise.all([api('api/state'), api('api/brains')]);
    const brains = (br && br.brains) || {};
    const names = ['chat', 'mc', 'mc_mod', 'pc', 'pvz'];
    const cards = names.map(n => {
      const s = brains[n];
      const present = s !== undefined;
      return `<div class="brain-card">
        <div class="bt"><span>${esc(n)}</span>${present ? badge(true, '运行中') : badge(false, '未加载')}</div>
        <pre>${present ? esc(pretty(s)) : '（该大脑未在当前核心注册）'}</pre>
        <div class="row" style="margin-top:8px">
          ${(n === 'mc' ? `<button class="btn sm" data-cmd="/mc自动">启动 MC</button><button class="btn sm ghost" data-cmd="/mc停止">停止</button>` : '')}
          ${(n === 'pc' ? `<button class="btn sm" data-cmd="/电脑做 打开浏览器搜一下猫娘">派活</button>` : '')}
          ${(n === 'pvz' ? `<button class="btn sm" data-cmd="/pvz玩">启动 PVZ</button><button class="btn sm ghost" data-cmd="/pvz停">停止</button>` : '')}
          ${(n === 'chat' ? `<button class="btn sm ghost" data-cmd="/状态">状态</button>` : '')}
        </div>
      </div>`;
    }).join('');
    c.innerHTML = `
      <div class="card">
        <h3>🧠 大脑注册表</h3>
        <div class="hint">每个大脑对应一项自主能力；下方按钮通过真实核心执行对应斜杠命令。</div>
        <div class="grid cols-2">${cards}</div>
      </div>
      <div class="card"><h3>核心总状态</h3><pre style="white-space:pre-wrap;font-size:12px">${esc(pretty(st.status || st))}</pre></div>`;
    $$('[data-cmd]', c).forEach(b => b.onclick = () => runCmd(b.dataset.cmd));
  },

  // 记忆
  async memory(c) {
    const d = await api('api/memory');
    const recent = (d.recent || []).map(m => `<div class="row" style="gap:6px"><span class="chip">${esc(m.key || '')}</span><b>${esc(m.role)}</b><span>${esc(m.text)}</span></div>`).join('') || '<div class="empty">暂无对话记录</div>';
    const notes = d.notes && typeof d.notes === 'object' ? Object.entries(d.notes).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(pretty(v))}</td></tr>`).join('') : '<tr><td>—</td><td>空</td></tr>';
    const refl = d.reflections && typeof d.reflections === 'object' ? Object.entries(d.reflections).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(pretty(v))}</td></tr>`).join('') : '<tr><td>—</td><td>空</td></tr>';
    c.innerHTML = `
      <div class="card"><h3>💾 最近对话片段</h3>${recent}</div>
      <div class="grid cols-2">
        <div class="card"><h3>重要事项 (important_notes)</h3><table class="kv">${notes}</table></div>
        <div class="card"><h3>反思记忆 (reflection)</h3><table class="kv">${refl}</table></div>
      </div>
      <div class="card"><h3>用户画像 / 人格记忆 / 摘要</h3>
        <div class="row"><span class="muted">画像用户：</span>${ (d.profiles_keys||[]).map(k=>`<span class="chip">${esc(k)}</span>`).join('') || '无' }</div>
        <div class="row" style="margin-top:8px"><span class="muted">摘要场景：</span>${ (d.summary_keys||[]).map(k=>`<span class="chip">${esc(k)}</span>`).join('') || '无' }</div>
        <pre style="white-space:pre-wrap;font-size:12px;margin-top:10px">${esc(pretty(d.persona))}</pre>
        <div class="mem-actions">
          <button data-mem-clear="reflection">清空反思</button>
          <button data-mem-clear="persona">清空人格</button>
          <button data-mem-clear="notes">清空记事</button>
        </div>
      </div>`;
      $$('[data-mem-clear]', c).forEach(b => b.onclick = () => memoryClear(b.dataset.memClear));
  },

  // 知识库
  async knowledge(c) {
    c.innerHTML = `
      <div class="card">
        <h3>📚 知识库 (knowledge_base.json)</h3>
        <input class="search" id="kbSearch" placeholder="搜索知识条目…" />
        <div id="kbList" style="margin-top:12px"></div>
      </div>`;
    const render = async (q) => {
      const d = await api('api/knowledge' + (q ? ('?q=' + encodeURIComponent(q)) : ''));
      const items = d.items || [];
      $('#kbList').innerHTML = `<div class="muted" style="margin-bottom:8px">共 ${d.count} 条，当前显示 ${items.length} 条</div>` +
        (items.map((it, i) => `<div class="card" style="padding:12px 14px;margin-bottom:10px">
            <div class="row"><b>#${i + 1}</b>${(it.id ? `<span class="chip">${esc(it.id)}</span>` : '')}${(it.category ? `<span class="chip">${esc(it.category)}</span>` : '')}</div>
            <div style="margin-top:6px">${esc(it.text || it.content || pretty(it))}</div>
          </div>`).join('') || '<div class="empty">无匹配条目</div>');
    };
    let tm; $('#kbSearch').addEventListener('input', (e) => { clearTimeout(tm); tm = setTimeout(() => render(e.target.value), 250); });
    render('');
  },

  // 情绪
  async emotion(c) {
    const d = await api('api/emotion');
    const mood = d && d.mood; const label = d && d.label; const events = (d && d.events) || [];
    c.innerHTML = `
      <div class="card">
        <h3>😊 当前心情</h3>
        <div class="row">
          <span class="muted">心情值 mood：</span><b>${esc(mood == null ? '—' : mood)}</b>
          &nbsp; <span class="muted">主导情绪：</span>${label ? `<span class="badge on">${esc(label.name || label)}</span>` : '<span class="badge idle">平静</span>'}
          <button class="btn sm ghost" id="emoReset" style="margin-left:auto">重置心情</button>
        </div>
        <pre style="white-space:pre-wrap;font-size:12px;margin-top:10px">${esc(pretty(d))}</pre>
      </div>
      <div class="card"><h3>情绪事件流水</h3>
        ${(events.length ? events.slice(-20).reverse().map(e => `<div class="row" style="gap:6px"><span class="chip">${esc(e.user || '?')}</span><span>${esc(e.text || pretty(e))}</span></div>`).join('') : '<div class="empty">暂无事件</div>')}
      </div>`;
    $('#emoReset').onclick = async () => {
      const r = await api('api/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'reset_emotion' }) });
      toast(r.ok ? '心情已重置' : ('失败：' + (r.error || '')));
      VIEWS.emotion(c);
    };
  },

  // 身份绑定
  async identity(c) {
    const d = await api('api/identity');
    const rows = d.data && typeof d.data === 'object' ? Object.entries(d.data).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(pretty(v))}</td></tr>`).join('') : '<tr><td>—</td><td>空</td></tr>';
    c.innerHTML = `<div class="card"><h3>🪪 身份绑定 (identity_bindings.json)</h3>
      <div class="muted" style="margin-bottom:8px">共 ${d.count || 0} 条：把 QQ / 平台账号映射到「同一个我」。</div>
      <table class="kv">${rows}</table></div>`;
  },

  // MC
  async mc(c) {
    const d = await api('api/mc');
    const live = d.live || {};
    const gs = d.game_state || {};
    const inv = (live.inventory || gs.inventory || []);
    const env = (live.env || gs.env || []);
    c.innerHTML = `
      <div class="card"><h3>🎮 我的世界 · 实时状态</h3>
        <div class="grid cols-3">
          <div><div class="muted">当前目标</div><b>${esc(live.goal || gs.goal || '—')}</b></div>
          <div><div class="muted">生存状态</div><b>${esc(live.survival || gs.survival || '—')}</b></div>
          <div><div class="muted">最近反馈</div><b>${esc(live.feedback || gs.feedback || '—')}</b></div>
        </div>
        <div class="row" style="margin-top:12px">
          <button class="btn sm" data-cmd="/mc自动">自动游玩</button>
          <button class="btn sm ghost" data-cmd="/mc停止">停止</button>
          <button class="btn sm ghost" data-cmd="/mc状态">状态</button>
          <button class="btn sm ghost" data-cmd="/mc技能">技能列表</button>
        </div>
        <details style="margin-top:12px"><summary class="muted">背包 (${inv.length})</summary>
          <div class="chips" style="margin-top:8px">${inv.map(x => `<span class="chip">${esc(x)}</span>`).join('') || '空'}</div></details>
        <details style="margin-top:8px"><summary class="muted">环境感知 (${env.length})</summary>
          <div class="chips" style="margin-top:8px">${env.map(x => `<span class="chip">${esc(x)}</span>`).join('') || '无'}</div></details>
      </div>
      <div class="card"><h3>技能库 (${ (d.skills||[]).length })</h3>
        <div class="chips">${(d.skills||[]).map(s => `<span class="chip">${esc(s.name || s)}</span>`).join('') || '空'}</div></div>
      <div class="card"><h3>给 MC 大脑发提示</h3>
        <div class="row">
          <input class="search" id="mcHint" placeholder="例如：去挖点铁、盖个房子…" style="flex:1" />
          <button class="btn" id="mcHintSend">发送提示</button>
        </div>
        <div class="muted" style="margin-top:10px">最近提示：</div>
        ${(d.recent_hints || []).slice(-12).reverse().map(h => `<div class="row" style="gap:6px"><span class="chip">${esc(h.user || h.time || '')}</span><span>${esc(h.text || pretty(h))}</span></div>`).join('') || '<div class="empty">暂无</div>'}
      </div>`;
    $$('[data-cmd]', c).forEach(b => b.onclick = () => runCmd(b.dataset.cmd));
    $('#mcHintSend').onclick = async () => {
      const t = $('#mcHint').value.trim(); if (!t) return;
      const r = await api('api/hint', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: t }) });
      toast(r.ok ? '提示已发送' : ('失败：' + (r.error || r.note || '')));
      $('#mcHint').value = ''; VIEWS.mc(c);
    };
  },

  // PC
  async pc(c) {
    const cfg = await api('api/config');
    const en = (k) => cfg && cfg[k];
    c.innerHTML = `
      <div class="card"><h3>🖥️ 电脑操控 (pc_agent)</h3>
        <div class="hint">把一句话任务交给猫娘，她会自己操作鼠标键盘 / 截屏理解。通过真实核心执行 <span class="kbd">/电脑做 ...</span>。</div>
        <div class="row">
          <input class="search" id="pcTask" placeholder="例如：打开记事本写一句「喵」并保存" style="flex:1" />
          <button class="btn" id="pcRun">派活</button>
        </div>
        <div class="row" style="margin-top:12px">
          <button class="btn sm ghost" data-cmd="/电脑状态">状态</button>
          <button class="btn sm ghost" data-cmd="/电脑截图">截图</button>
        </div>
        <div class="muted" style="margin-top:12px">能力开关：</div>
        <div class="chips" style="margin-top:6px">
          ${badge(en('ENABLE_PC_AGENT'))} PC_AGENT
          ${badge(en('ENABLE_SCREEN_AWARENESS'))} SCREEN_AWARENESS
          ${badge(en('ENABLE_LIVE_VISION'))} LIVE_VISION
          ${badge(en('ENABLE_DXCAM'))} DXCAM
        </div>
      </div>`;
    $('#pcRun').onclick = () => {
      const t = $('#pcTask').value.trim(); if (!t) return;
      runCmd('/电脑做 ' + t); $('#pcTask').value = '';
    };
    $$('[data-cmd]', c).forEach(b => b.onclick = () => runCmd(b.dataset.cmd));
  },

  // PVZ
  async pvz(c) {
    const d = await api('api/pvz');
    const shots = (d.shots || []).map(s => `<figure><img src="${esc(s.url)}" loading="lazy" /><figcaption>${esc(s.name)} · ${esc(s.mtime)}</figcaption></figure>`).join('') || '<div class="empty">暂无截图</div>';
    c.innerHTML = `
      <div class="card"><h3>🌻 植物大战僵尸 (pvz_agent)</h3>
        <div class="row">
          <button class="btn sm" data-cmd="/pvz玩">启动自动游玩</button>
          <button class="btn sm ghost" data-cmd="/pvz停">停止</button>
          <button class="btn sm ghost" data-cmd="/pvz状态">状态</button>
          <button class="btn sm ghost" data-cmd="/pvz攻略">攻略</button>
        </div>
        <div class="muted" style="margin-top:8px">状态文件：${d.state_file ? '✅ pvz_state_now.png 存在' : '无'}</div>
      </div>
      <div class="card"><h3>最新截图墙</h3><div class="shots">${shots}</div></div>`;
    $$('[data-cmd]', c).forEach(b => b.onclick = () => runCmd(b.dataset.cmd));
  },

  // B站直播
  async bili(c) {
    const [b, cfg] = await Promise.all([api('api/bili'), api('api/config')]);
    c.innerHTML = `
      <div class="card"><h3>📡 B 站直播 (bilibili_plugin)</h3>
        <table class="kv">
          <tr><td>ENABLE_BILI_LIVE</td><td>${badge(b.ENABLE_BILI_LIVE)}</td></tr>
          <tr><td>房间号</td><td>${esc(b.BILI_ROOM_ID || '—')}</td></tr>
          <tr><td>UID</td><td>${esc(b.BILI_UID || '—')}</td></tr>
          <tr><td>标题</td><td>${esc(b.BILI_LIVE_TITLE || '—')}</td></tr>
          <tr><td>直播模式</td><td>${esc(cfg.BILIBILI_MODE || '—')}</td></tr>
          <tr><td>自动开播</td><td>${badge(cfg.BILIBILI_AUTO_START)}</td></tr>
        </table>
        <div class="row" style="margin-top:12px">
          <button class="btn sm" data-cmd="/直播 开播">开播</button>
          <button class="btn sm ghost" data-cmd="/直播 下播">下播</button>
          <button class="btn sm ghost" data-cmd="/直播 状态">状态</button>
        </div>
      </div>`;
    $$('[data-cmd]', c).forEach(b => b.onclick = () => runCmd(b.dataset.cmd));
  },

  // 语音 / TTS
  async voice(c) {
    const v = await api('api/voice');
    c.innerHTML = `
      <div class="card"><h3>🔊 语音 / TTS</h3>
        <table class="kv">
          <tr><td>ENABLE_VOX</td><td>${badge(v.ENABLE_VOX)}</td></tr>
          <tr><td>ENABLE_VOX_COSY</td><td>${badge(v.ENABLE_VOX_COSY)}</td></tr>
          <tr><td>ENABLE_REALTIME_VOICE</td><td>${badge(v.ENABLE_REALTIME_VOICE)}</td></tr>
          <tr><td>ENABLE_TTS_AFTER_REPLY</td><td>${badge(v.ENABLE_TTS_AFTER_REPLY)}</td></tr>
          <tr><td>VOX_VOICE</td><td>${esc(pretty(v.VOX_VOICE))}</td></tr>
        </table>
        <div class="row" style="margin-top:12px">
          <button class="btn sm" id="ttsTest">浏览器朗读测试</button>
          <button class="btn sm ghost" data-cmd="/语音测试">引擎语音测试</button>
        </div>
        <div class="muted" style="margin-top:8px">说明：引擎语音由 VOX/CosyVoice 在服务端合成；「浏览器朗读测试」用 Web Speech 直接朗读示例。</div>
      </div>`;
    $('#ttsTest').onclick = () => {
      if (!('speechSynthesis' in window)) return toast('浏览器不支持语音合成');
      const u = new SpeechSynthesisUtterance('喵~ 我是肥鱼娘，今天也要元气满满哦！');
      u.lang = 'zh-CN'; speechSynthesis.cancel(); speechSynthesis.speak(u);
    };
    $$('[data-cmd]', c).forEach(b => b.onclick = () => runCmd(b.dataset.cmd));
  },

  // 视觉
  async vision(c) {
    const cfg = await api('api/config');
    const keys = ['ENABLE_SCREEN_AWARENESS', 'ENABLE_LIVE_VISION', 'ENABLE_LOCAL_VL', 'ENABLE_DXCAM', 'ENABLE_VISION_CAPTURE', 'PRELOAD_LOCAL_VL'];
    const rows = keys.map(k => `<tr><td>${esc(k)}</td><td>${badge(cfg[k])}</td></tr>`).join('');
    c.innerHTML = `
      <div class="card"><h3>👁️ 视觉 / 屏幕感知</h3>
        <div class="hint">让猫娘「看」屏幕（截图理解 / 实时视觉 / 本地视频理解）。相关能力开关如下：</div>
        <table class="kv">${rows}</table>
        <div class="row" style="margin-top:12px">
          <button class="btn sm ghost" data-cmd="/看屏幕">看一眼屏幕</button>
          <button class="btn sm ghost" data-cmd="/视觉状态">视觉状态</button>
        </div>
      </div>`;
    $$('[data-cmd]', c).forEach(b => b.onclick = () => runCmd(b.dataset.cmd));
  },

  // 配置
  async config(c) {
    const d = await api('api/config');
    const keys = Object.keys(d).filter(k => !k.startsWith('__'));
    const rows = keys.map(k => `<tr><td>${esc(k)}</td><td>${esc(pretty(d[k]))}</td></tr>`).join('');
    c.innerHTML = `<div class="card"><h3>⚙️ 运行配置 (config.py)</h3>
      <div class="muted" style="margin-bottom:8px">密钥已脱敏显示。修改需编辑 config.py 后重启。</div>
      <table class="kv">${rows}</table></div>`;
  },

  // 系统 / 日志
  async system(c) {
    const [sys, st] = await Promise.all([api('api/system'), api('api/state')]);
    const logs = (sys.logs || []).map(l => esc(l)).join('\n') || '（无日志文件 bot.log）';
    c.innerHTML = `
      <div class="card"><h3>🛡️ 运行状态</h3>
        <div class="row">
          <span class="muted">核心：</span>${badge(sys.core, '已启动', '未启动')}
          <span class="muted">时间：</span><b>${esc(sys.time)}</b>
          <span class="muted">日志行数：</span><b>${esc(sys.uptime_log_lines)}</b>
        </div>
        <pre style="white-space:pre-wrap;font-size:12px;margin-top:10px">${esc(pretty(st.status || st))}</pre>
      </div>
      <div class="card"><h3>日志 (bot.log 末尾 50 行)</h3><div class="logbox">${logs}</div></div>`;
  },

  // 插件 / 服务管理
  plugins(c) {
    c.innerHTML = `<div class="card"><h3>🧩 插件 / 服务管理</h3>
      <div class="hint">统一管理平台插件、功能插件与本地服务（sidecar）。sidecar 重启需由 <code>launcher.py</code> / <code>start.py</code> 启动。</div>
      <div id="plugsBody" class="empty">加载中…</div></div>`;
    loadPlugins(c);
  },

  // 记忆浏览
  async memory_browser(c) {
    c.innerHTML = `<div class="card"><h3>🧠 记忆浏览</h3>
      <div class="hint">浏览并管理长期记忆：反思、交互规则、人格画像、记事。可单条删除或整类清空（清空后不可恢复）。</div>
      <div id="mbBody" class="empty">加载中…</div></div>`;
    loadMemoryBrowser(c);
  }
};

// 加载并渲染插件 / 服务聚合数据
async function loadPlugins(c) {
  const body = $('#plugsBody');
  if (!body) return;
  const d = await api('api/plugins');
  if (d.error) { body.innerHTML = `<div class="err">加载失败：${esc(d.error)}</div>`; return; }
  let h = '';
  h += `<h4>本地服务 (sidecar)</h4>`;
  h += `<table class="tb"><thead><tr><th>名称</th><th>脚本</th><th>运行</th><th>就绪</th><th>PID</th><th>端口</th><th>运行时</th><th>操作</th></tr></thead><tbody>`;
  (d.sidecars || []).forEach(s => {
    h += `<tr><td><b>${esc(s.name)}</b></td><td><code>${esc(s.script)}</code></td>`
      + `<td>${badge(s.running)}</td><td>${badge(s.ready)}</td><td>${esc(s.pid || '—')}</td>`
      + `<td>${esc(s.port)}</td><td>${esc(s.python)}</td>`
      + `<td>${s.running ? `<button class="btn sm" data-restart="${esc(s.name)}">重启</button>` : '<span class="muted">—</span>'}</td></tr>`;
  });
  if (!d.sidecars || !d.sidecars.length)
    h += `<tr><td colspan="8" class="muted">未启用（在 config.py 设 ENABLE_MEMORY_SERVER / ENABLE_MONITOR / ENABLE_TTS_SERVER=True 并由 launcher 启动）</td></tr>`;
  h += `</tbody></table>`;

  h += `<h4>平台插件</h4><table class="tb"><thead><tr><th>名称</th><th>平台</th><th>运行</th><th>能力</th></tr></thead><tbody>`;
  (d.platforms || []).forEach(p => {
    const caps = Object.keys(p.capabilities || {}).filter(k => p.capabilities[k]).join(', ') || '—';
    h += `<tr><td><b>${esc(p.name)}</b></td><td>${esc(p.platform)}</td><td>${badge(p.running)}</td><td>${esc(caps)}</td></tr>`;
  });
  if (!d.platforms || !d.platforms.length) h += `<tr><td colspan="4" class="muted">（核心未连接）</td></tr>`;
  h += `</tbody></table>`;

  h += `<h4>功能插件</h4><table class="tb"><thead><tr><th>名称</th><th>运行</th></tr></thead><tbody>`;
  (d.features || []).forEach(f => {
    h += `<tr><td><b>${esc(f.name)}</b></td><td>${badge(f.running)}</td></tr>`;
  });
  if (!d.features || !d.features.length) h += `<tr><td colspan="2" class="muted">（无 / 核心未连接）</td></tr>`;
  h += `</tbody></table>`;

  h += `<h4>功能开关 (config.py)</h4><table class="tb"><thead><tr><th>配置项</th><th>当前</th></tr></thead><tbody>`;
  (d.toggles || []).forEach(t => {
    h += `<tr><td><code>${esc(t.key)}</code></td><td>${badge(t.value)}</td></tr>`;
  });
  h += `</tbody></table>`;

  if (!d.launcher) h += `<div class="muted" style="margin-top:8px">提示：当前未由 launcher 托管，sidecar 重启不可用。请用 <code>python start.py</code> 或 <code>python launcher.py</code> 启动以统一管理。</div>`;
  body.innerHTML = h;
  body.querySelectorAll('[data-restart]').forEach(b => {
    b.onclick = async () => {
      b.disabled = true; b.textContent = '重启中…';
      const r = await api('api/plugins/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'restart_sidecar', target: b.dataset.restart }) });
      toast(r.ok ? `已发送重启：${b.dataset.restart}` : ('失败：' + (r.error || '')));
      setTimeout(() => loadPlugins(c), 1800);
    };
  });
}

// 加载并渲染记忆浏览器
async function loadMemoryBrowser(c) {
  const body = $('#mbBody');
  if (!body) return;
  const d = await api('api/memory_browser');
  if (d.error) { body.innerHTML = `<div class="err">加载失败：${esc(d.error)}</div>`; return; }
  const refls = d.reflections || [];
  const rules = d.rules || [];
  const pers = d.personas || [];
  const notes = d.notes || [];

  const table = (title, rows, emptyMsg) => {
    if (!rows.length) return `<h4>${title}</h4><div class="empty">${emptyMsg || '暂无'}</div>`;
    return `<h4>${title}（${rows.length}）</h4><table class="tb"><thead><tr><th>内容</th><th>附加信息</th><th>标识 / 时间</th><th>操作</th></tr></thead><tbody>${rows.join('')}</tbody></table>`;
  };

  const reflRows = refls.map(r => `<tr><td>${esc(r.content)}</td><td>${esc(r.type || '')}</td><td>${esc(r.user_id || '')} · ${esc(r.ts || '')}</td><td><button class="btn sm ghost" data-mb-del="reflection" data-mb-ts="${esc(r.ts || '')}" data-mb-content="${esc(r.content || '')}">删除</button></td></tr>`);
  const ruleRows = rules.map(r => `<tr><td>${esc(r.rule)}</td><td>置信 ${esc(r.confidence == null ? '—' : r.confidence)}</td><td>—</td><td><button class="btn sm ghost" data-mb-del="rule" data-mb-rule="${esc(r.rule || '')}">删除</button></td></tr>`);
  const persRows = pers.map(p => `<tr><td><b>${esc(p.user_id || '')}</b></td><td>${esc(pretty({ traits: p.traits, style: p.style }))}</td><td>${esc(p.updated || '')}</td><td><button class="btn sm ghost" data-mb-del="persona" data-mb-uid="${esc(p.user_id || '')}">删除</button></td></tr>`);
  const noteRows = notes.map(n => `<tr><td><b>${esc(n.user_id || '')}#${esc(n.index || '')}</b></td><td>${esc(n.text || '')}</td><td>${esc(n.time || '')}</td><td><button class="btn sm ghost" data-mb-del="note" data-mb-uid="${esc(n.user_id || '')}" data-mb-index="${esc(n.index || '')}">删除</button></td></tr>`);

  let h = '';
  h += table('反思记忆 (reflection)', reflRows, '暂无反思记录');
  h += table('交互规则 (interaction rules)', ruleRows, '暂无交互规则');
  h += table('人格画像 (persona)', persRows, '暂无人格画像');
  h += table('记事 (notes)', noteRows, '暂无记事');

  h += `<div class="row" style="margin-top:10px;gap:14px">
    <span class="muted">统计：</span>
    反思 <b>${refls.length}</b> · 规则 <b>${rules.length}</b> · 人格 <b>${pers.length}</b> · 记事 <b>${notes.length}</b>
  </div>`;
  h += `<div class="mem-actions" style="margin-top:10px">
    <button class="btn sm ghost" data-mb-clear="reflections">清空反思</button>
    <button class="btn sm ghost" data-mb-clear="rules">清空规则</button>
    <button class="btn sm ghost" data-mb-clear="personas">清空人格</button>
    <button class="btn sm ghost" data-mb-clear="notes">清空记事</button>
  </div>`;
  body.innerHTML = h;

  body.querySelectorAll('[data-mb-del]').forEach(b => {
    b.onclick = async () => {
      const kind = b.dataset.mbDel;
      const payload = {};
      if (kind === 'reflection') { payload.action = 'delete_reflection'; payload.ts = b.dataset.mbTs; payload.content = b.dataset.mbContent; }
      else if (kind === 'rule') { payload.action = 'delete_rule'; payload.rule = b.dataset.mbRule; }
      else if (kind === 'persona') { payload.action = 'delete_persona'; payload.user_id = b.dataset.mbUid; }
      else if (kind === 'note') { payload.action = 'delete_note'; payload.user_id = b.dataset.mbUid; payload.index = Number(b.dataset.mbIndex); }
      const r = await api('api/memory_browser', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
      toast(r.ok ? '已删除' : ('失败：' + (r.error || '')));
      loadMemoryBrowser(c);
    };
  });
  body.querySelectorAll('[data-mb-clear]').forEach(b => {
    b.onclick = async () => {
      if (!confirm('确定清空 ' + b.dataset.mbClear + '？此操作不可恢复。')) return;
      const r = await api('api/memory_browser', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'clear_' + b.dataset.mbClear }) });
      toast(r.ok ? '已清空' : ('失败：' + (r.error || '')));
      loadMemoryBrowser(c);
    };
  });
}

function renderChatLog() {
  const log = $('#chatLog');
  if (!log) return;
  let html = chatStore.map(m => {
    if (m.role === 'me') return `<div class="msg me"><div class="ava">🐱</div><div class="bubble">${esc(m.text)}</div></div>`;
    let inner = '';
    if (m.text) inner += esc(m.text);
    if (m.image) inner += `<br><img class="shot" src="${esc(m.image)}" />`;
    if (m.audio) inner += `<br><audio controls src="${esc(m.audio)}"></audio>`;
    return `<div class="msg${m.proactive ? ' proactive' : ''}"><div class="ava">${m.proactive ? '💡' : '🐟'}</div><div class="bubble">${inner}</div></div>`;
  }).join('');
  if (typing) html += `<div class="msg typing"><div class="ava">🐟</div><div class="bubble">肥鱼娘正在思考…</div></div>`;
  log.innerHTML = html;
  log.scrollTop = log.scrollHeight;
}

// ---------- 启动 ----------
$$('.nav-item').forEach(b => b.onclick = () => switchView(b.dataset.view));
connect();

// 记忆校对：清空某类记忆后刷新记忆页
async function memoryClear(type) {
  if (!confirm('确定清空' + type + '记忆？')) return;
  try {
    const r = await fetch('/api/memory', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: 'clear_' + type}),
    });
    const j = await r.json();
    if (!j.ok) alert('清空失败：' + (j.error || '未知错误'));
  } catch (e) {
    alert('请求失败：' + e);
  }
  switchView('memory');
}
setInterval(() => api('api/health').then(r => r.ok ? setConn('on', '已连接') : setConn('off', '核心未响应')).catch(() => setConn('off', '连接失败')), 15000);
switchView('chat');
