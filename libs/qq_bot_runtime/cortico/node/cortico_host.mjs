/**
 * Cortico World 的 Node 宿主（JSON-RPC over stdio）。
 *
 * Python 侧（`node_bridge.py`）启动本脚本，用换行分隔的 JSON 通信：
 *   Python -> Node : {"id":1,"method":"...","params":{...}}
 *   Node -> Python : {"id":1,"result":...} | {"id":1,"error":"..."}
 *   Node -> Python : {"id":2,"method":"host.pushEvent","params":{...}}   （回调，等应答）
 *   Node -> Python : {"method":"host.log","params":{...}}                （通知，不等应答）
 *
 * 本脚本只做「加载 TS 包 + 转发调用」，不实现任何 Core 能力：宿主能力全部回调给 Python。
 *
 * 用法：node --import ./cortico_loader.mjs cortico_host.mjs <packageDir>
 * 环境变量：CORTICO_CORE_ROOT（Cortico 仓库根，用于解析 `cortico/...` 裸导入）
 */
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const PKG_DIR = process.argv[2];
if (!PKG_DIR) {
  fail('usage: cortico_host.mjs <packageDir>');
}

// ---------------------------------------------------------------------------
// stdio JSON-RPC
// ---------------------------------------------------------------------------
let seq = 0;
const pending = new Map(); // id -> {resolve, reject}
const handlers = new Map(); // 由 Python 调用的方法

function send(msg) {
  try {
    process.stdout.write(JSON.stringify(msg) + '\n');
  } catch (err) {
    process.stderr.write(`send failed: ${err}\n`);
  }
}

function request(method, params = {}, timeoutMs = 30000) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`${method} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    pending.set(id, {
      resolve: (v) => { clearTimeout(timer); resolve(v); },
      reject: (e) => { clearTimeout(timer); reject(e); },
    });
    send({ id, method, params });
  });
}

function notify(method, params = {}) {
  send({ method, params });
}

let buffer = '';
process.stdin.on('data', (chunk) => {
  buffer += chunk.toString('utf8');
  let idx;
  while ((idx = buffer.indexOf('\n')) >= 0) {
    const line = buffer.slice(0, idx).trim();
    buffer = buffer.slice(idx + 1);
    if (!line) continue;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      continue;
    }
    handleIncoming(msg);
  }
});
process.stdin.on('end', () => shutdown(0));

async function handleIncoming(msg) {
  // 应答
  if (msg && typeof msg.id === 'number' && (('result' in msg) || ('error' in msg))) {
    const p = pending.get(msg.id);
    if (!p) return;
    pending.delete(msg.id);
    if ('error' in msg) p.reject(new Error(String(msg.error)));
    else p.resolve(msg.result);
    return;
  }
  // 请求
  if (msg && msg.method) {
    const fn = handlers.get(msg.method);
    if (!fn) {
      if (typeof msg.id === 'number') send({ id: msg.id, error: `unknown method ${msg.method}` });
      return;
    }
    try {
      const result = await fn(msg.params || {});
      if (typeof msg.id === 'number') send({ id: msg.id, result: result ?? null });
    } catch (err) {
      if (typeof msg.id === 'number') send({ id: msg.id, error: String(err && err.message || err) });
    }
  }
}

function fail(message) {
  process.stderr.write(`${message}\n`);
  process.exit(1);
}

// ---------------------------------------------------------------------------
// 宿主能力代理：把 TS WorldHost 的调用转给 Python
// ---------------------------------------------------------------------------
function makeHost(worldId) {
  return {
    async pushEvent(e, opts = {}) {
      const saved = await request('host.pushEvent', {
        type: e.type,
        ts: e.ts,
        source: e.source || worldId,
        origin: e.origin || 'external',
        tags: e.tags || [],
        senderKey: e.senderKey || '',
        meta: e.meta || {},
        text: e.text,
        deliver: opts.deliver !== false,
        trigger: opts.trigger || null,
      }, 30000);
      return saved || e;
    },
    pushDeferred(spec, opts = {}) {
      // 渲染失败/返回 null 时静默丢弃（与 Core 一致）
      try {
        const out = spec && typeof spec.render === 'function' ? spec.render() : null;
        if (out == null) return;
        const text = typeof out === 'string' ? out : String(out.text ?? out);
        notify('host.pushDeferred', {
          type: spec.type, source: spec.source || worldId, origin: spec.origin || 'external',
          senderKey: spec.senderKey || '', meta: spec.meta || {}, text, trigger: opts?.trigger || null,
        });
      } catch (err) {
        notify('host.log', { level: 'warn', message: `pushDeferred 渲染失败: ${err}` });
      }
    },
    async drainPendingEvents() {
      // Python 侧按谓词过滤；这里无法传函数，返回全部由调用方自行过滤
      const list = await request('host.drainPending', { source: worldId }, 15000);
      return Array.isArray(list) ? list : [];
    },
    get store() {
      return {
        since: async (cursor = 0, limit = 200) => {
          const list = await request('host.storeSince', { cursor, limit }, 15000);
          return Array.isArray(list) ? list : [];
        },
        latest: () => request('host.storeLatest', {}, 10000),
        count: () => request('host.storeCount', {}, 10000),
      };
    },
    get modelFacts() {
      return {
        model: () => '',
        accepts: () => false,
        contextWindow: () => undefined,
      };
    },
    blob: () => null,
    reportUsage: (usage) => notify('host.reportUsage', usage || {}),
    isPaused: () => false,
    log: {
      error: (m, d) => notify('host.log', { level: 'error', message: String(m), data: d }),
      warn: (m, d) => notify('host.log', { level: 'warn', message: String(m), data: d }),
      info: (m, d) => notify('host.log', { level: 'info', message: String(m), data: d }),
      debug: (m, d) => notify('host.log', { level: 'debug', message: String(m), data: d }),
    },
    async llmStalls() { return 0; },
  };
}

// ---------------------------------------------------------------------------
// 加载扩展包
// ---------------------------------------------------------------------------
let definition = null;
let world = null;
let ctxCfg = {};
const secrets = new Map();

async function loadDefinition() {
  const entry = pathToFileURL(path.resolve(PKG_DIR)).href + '/';
  let mod;
  try {
    mod = await import(entry);
  } catch (err) {
    // main 指向 src/index.ts 时按 exports/main 再试一次
    const pkg = await import(pathToFileURL(path.join(PKG_DIR, 'package.json')).href, { with: { type: 'json' } }).catch(() => null);
    const main = pkg?.default?.main || pkg?.main;
    if (!main) throw err;
    mod = await import(new URL(main, pathToFileURL(path.join(PKG_DIR, 'x')).href).href);
  }
  const def = mod.default ?? mod.DUNGEON ?? Object.values(mod).find(
    (v) => v && typeof v === 'object' && typeof v.id === 'string' && typeof v.create === 'function');
  if (!def) throw new Error('包没有导出默认的 WorldDefinition');
  return def;
}

function makeContext(p) {
  return {
    id: p.id,
    cfg: ctxCfg,
    timezone: p.timezone || '',
    botName: p.botName || '',
    botDir: p.botDir || '',
    packageDir: PKG_DIR,
    dataDir: p.dataDir || '',
    repoRoot: p.repoRoot || '',
    secret: (name) => secrets.get(name) ?? '',
    storeSecret: (name, value) => {
      secrets.set(name, value);
      notify('host.storeSecret', { name, value });
    },
    persist: (patch) => notify('host.persist', { patch: patch || {} }),
    restart: async () => { await request('host.restart', {}, 60000); },
  };
}

// ---------------------------------------------------------------------------
// Python 可调方法
// ---------------------------------------------------------------------------
handlers.set('hello', async () => ({
  ok: true,
  package: path.basename(PKG_DIR),
  worldId: definition ? definition.id : null,
  label: definition ? (definition.label || definition.id) : null,
}));

handlers.set('create', async (p) => {
  if (!definition) definition = await loadDefinition();
  ctxCfg = p.cfg || {};
  for (const [k, v] of Object.entries(p.secrets || {})) secrets.set(k, v);
  world = definition.create ? definition.create(makeContext(p)) : null;
  if (!world) throw new Error('create() 没有返回 World 实例');
  return { id: world.id };
});

handlers.set('start', async () => {
  if (!world) throw new Error('尚未 create');
  await world.start(makeHost(world.id));
  return { ok: true };
});

handlers.set('stop', async () => {
  if (world) await world.stop();
  return { ok: true };
});

handlers.set('tools', async () => {
  if (!world) return [];
  return (world.tools() || []).map((t) => ({
    name: t.name,
    description: t.description || '',
    parameters: t.parameters || { type: 'object', properties: {} },
    tags: t.tags || [],
    barrierAfter: !!t.barrierAfter,
    endsTurn: !!t.endsTurn,
  }));
});

handlers.set('callTool', async (p) => {
  if (!world) throw new Error('尚未 create');
  const decl = (world.tools() || []).find((t) => t.name === p.name);
  if (!decl) throw new Error(`未知工具 ${p.name}`);
  const ctx = {
    role: p.role || 'main',
    callId: p.callId || '',
    round: p.round || 0,
    signal: p.abort ? { aborted: true } : undefined,
    log: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  };
  const out = await decl.handler(p.args || {}, ctx);
  if (typeof out === 'string') return { text: out };
  return {
    text: String(out?.text ?? ''),
    failed: !!out?.failed,
    next: Array.isArray(out?.next) ? out.next : [],
  };
});

handlers.set('envPromptVars', async () => {
  if (!world) return null;
  const v = await world.envPromptVars();
  return v ?? null;
});

handlers.set('console', async (p) => {
  if (!world || typeof world.console !== 'function') return null;
  const decl = world.console(p.language || 'zh');
  if (!decl) return null;
  const invoke = decl.invoke;
  return {
    label: decl.label || '',
    lamps: decl.lamps || [],
    badges: decl.badges || [],
    panels: decl.panels || [],
    promptDocs: decl.promptDocs || [],
    config: decl.config || [],
    links: decl.links || [],
    hasInvoke: typeof invoke === 'function',
  };
});

handlers.set('invoke', async (p) => {
  if (!world || typeof world.console !== 'function') throw new Error('World 没有控制台');
  const decl = world.console(p.language || 'zh');
  if (!decl || typeof decl.invoke !== 'function') throw new Error('World 不支持 invoke');
  return await decl.invoke(p.panel, p.method, p.args || []);
});

handlers.set('defaults', async () => {
  if (!definition) definition = await loadDefinition();
  return typeof definition.defaults === 'function' ? definition.defaults() : { enabled: false };
});

handlers.set('shutdown', async () => {
  await shutdown(0);
  return { ok: true };
});

// ---------------------------------------------------------------------------
// 退出
// ---------------------------------------------------------------------------
let shuttingDown = false;
async function shutdown(code) {
  if (shuttingDown) return;
  shuttingDown = true;
  if (world) {
    try { await world.stop(); } catch { /* 忽略关闭期异常 */ }
  }
  process.exit(code);
}

process.on('SIGTERM', () => shutdown(0));
process.on('SIGINT', () => shutdown(0));

// 启动：先解析清单，让 hello 立刻可用
loadDefinition().catch((err) => {
  process.stderr.write(`加载扩展包失败: ${err && err.stack || err}\n`);
  process.exit(2);
});
