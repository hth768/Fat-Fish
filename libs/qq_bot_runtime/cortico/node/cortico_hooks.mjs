/**
 * 模块解析钩子（由 cortico_loader.mjs 通过 `register()` 注册）。
 *
 * 把扩展包里的 `cortico/...` 裸导入映射到 Cortico 仓库源码。cortico-world-* 包把
 * `cortico` 声明为 devDependency，自己的 node_modules 里通常没有它（core 由宿主提供），
 * 而 ESM 不走 NODE_PATH，所以需要解析钩子。
 *
 * 同步与异步两条链都要导出：tsx 解析 .ts 里的导入走同步链，只导出 `resolve` 会漏。
 */
import { pathToFileURL } from 'node:url';
import { existsSync } from 'node:fs';
import path from 'node:path';

const coreRoot = process.env.CORTICO_CORE_ROOT || '';

function mapCortico(specifier) {
  if (!coreRoot) return null;
  if (specifier !== 'cortico' && !specifier.startsWith('cortico/')) return null;
  const rest = specifier === 'cortico' ? 'index.ts' : specifier.slice('cortico'.length + 1);
  const target = path.join(coreRoot, 'src', rest);
  if (!existsSync(target)) return null;
  if (process.env.CORTICO_DEBUG_LOADER === '1') {
    process._rawDebug(`[cortico_hooks] ${specifier} -> ${target}`);
  }
  return { url: pathToFileURL(target).href, shortCircuit: true, format: 'module' };
}

export function resolve(specifier, context, nextResolve) {
  const mapped = mapCortico(specifier);
  return mapped || nextResolve(specifier, context);
}

export function resolveSync(specifier, context, nextResolve) {
  const mapped = mapCortico(specifier);
  return mapped || nextResolve(specifier, context);
}
