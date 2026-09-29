/**
 * 钩子注册入口：`node --import ./cortico_loader.mjs ...`
 *
 * Node 的模块定制钩子必须经 `register()` 注册（仅导出 resolve 不会被 --import 采用），
 * 所以这里只做注册，真正的解析逻辑在 cortico_hooks.mjs。
 */
import { register } from 'node:module';

register('./cortico_hooks.mjs', import.meta.url);
