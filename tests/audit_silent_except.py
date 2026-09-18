# -*- coding: utf-8 -*-
"""静默异常审计工具（AST 级）。

用途：把「`except ...: pass/continue` 吞掉异常、事后无痕迹」的位置全部找出来，
按证据分桶，供分层改造（见 DEGRADE_AUDIT.md）逐条处理。

    python tests/audit_silent_except.py                  # 只出报告
    python tests/audit_silent_except.py --json out.json  # 附机器可读清单
    python tests/audit_silent_except.py --convert bridge # 代码改写（默认 dry-run）
    python tests/audit_silent_except.py --convert bridge --write   # 真正落盘

分桶规则（依据 try 体内的证据，不猜语义；无法判断一律进 C 等人工看）：
  A 探测/回落类：try 体在 import / os.environ / getattr / exists / get / load / read 等
                 「探测或取值」形态 → 大概率是刻意的可选依赖探测与默认值回落。
  B 静默失败风险：try 体内有 save/write/dump/post/send/remove/kill/push/emit/notify 等
                 副作用动作 → 吞掉后外部完全不知道没做成，需要人判断是否该上报。
  C 需人工判定：其余。

改写规则（仅 A/C 桶、且 handler 体只有 pass / continue / ... 时）：
  把 `pass` 换成 `degrade("<文件>:<行> <函数>", e, "降级：<try 首句>")`，
  handler 未绑定异常时补 `as e`；同时在文件顶部导入区补 `from quiet import degrade`。
  B 桶**不改**（需人工判断该 degrade 还是 attention，甚至改成抛错）。
"""

import argparse
import ast
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".git", "node_modules", "__pycache__", "dist", "data", "venv", "runtime",
             "voice_pack", "vl_pack", "libs_pkgs", "site-packages", "feiyu-android",
             ".codebuddy", "hf_cache", "models", "logs", "音色试听", "_backup_20260918_logfix"}

# 探测/回落特征（属性或函数名）
PROBE_NAMES = {"environ", "getenv", "getattr", "get", "load", "loads", "read", "readlines",
               "exists", "isdir", "isfile", "getsize", "getmtime", "decode", "parse",
               "open", "find_spec", "import_module", "version", "todict", "fromstring",
               "listdir", "scandir", "stat", "guess_type", "expanduser", "abspath"}
# 副作用特征（静默失败风险）
SIDE_EFFECT_NAMES = {"save", "write", "writelines", "dump", "dumps", "post", "put", "send",
                     "publish", "upload", "remove", "unlink", "rmtree", "rmdir", "kill",
                     "terminate", "push", "emit", "notify", "record", "report", "execute",
                     "run", "call", "start", "stop", "register", "unregister", "delete",
                     "update", "insert", "commit", "flush", "close", "speak", "play"}


def _call_names(node: ast.AST) -> set:
    """收集子树里出现的属性名/函数名（用于特征判断）。"""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out.add("__import__")
    return out


def _rel(path: str) -> str:
    """相对仓库路径；跨盘（如测试用 C: 临时文件）时退回文件名。"""
    try:
        return os.path.relpath(path, REPO).replace("\\", "/")
    except ValueError:
        return os.path.basename(path)


def _first_stmt_snippet(try_body: list, src_lines: list, maxlen: int = 70) -> str:
    """取 try 体首句的**第一行**摘要（多行结构只取头一行，避免糊成一团）。"""
    if not try_body:
        return ""
    node = try_body[0]
    try:
        seg = ast.get_source_segment("\n".join(src_lines), node) or ""
    except Exception:
        seg = ""
    seg = seg.splitlines()[0] if seg else ""
    seg = re.sub(r"\s+", " ", seg).strip().rstrip(":")
    if isinstance(node, ast.Delete):
        seg = "del ..." + seg[:40]
    return seg[:maxlen]


def _is_silent(handler: ast.ExceptHandler) -> bool:
    """handler 体是否「静默」（只有 pass / continue / ...）。"""
    body = [n for n in handler.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str))]        # 允许纯字符串（文档串）
    if not body:
        return True
    if len(body) == 1:
        n = body[0]
        if isinstance(n, ast.Pass):
            return True
        if isinstance(n, ast.Continue):
            return True
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and n.value.value is Ellipsis:
            return True
    return False


def _outermost_call_names(try_body: list) -> set:
    """try 体里「最外层调用」的名字集合。

    链式调用 `open('a','w').write('x')` 的最外层是 `write`（真正的动作），
    内层的 `open` 只是前置步骤 —— 只按名字全集判断会把这类误判成「探测」。
    """
    out = set()
    for st in try_body:
        nodes = list(ast.walk(st))
        parents = {}
        for p in nodes:
            for c in ast.iter_child_nodes(p):
                parents[c] = p
        for n in nodes:
            if not isinstance(n, ast.Call):
                continue
            p = parents.get(n)
            nested = isinstance(p, ast.Call)
            if not nested and isinstance(p, ast.Attribute):
                nested = isinstance(parents.get(p), ast.Call)
            if nested:
                continue
            f = n.func
            nm = (f.attr if isinstance(f, ast.Attribute)
                  else f.id if isinstance(f, ast.Name) else "")
            if nm:
                out.add(nm)
    return out


def _bucket(handler: ast.ExceptHandler, try_body: list) -> str:
    names = set()
    for st in try_body:
        names |= _call_names(st)
    hard_se = names & SIDE_EFFECT_NAMES
    probe = names & PROBE_NAMES
    primary = _outermost_call_names(try_body)
    has_import = any(isinstance(n, (ast.Import, ast.ImportFrom))
                     for st in try_body for n in ast.walk(st))
    # 最外层动作就是写/发/删 → 静默失败风险最高，优先归 B
    if primary & SIDE_EFFECT_NAMES:
        return "B"
    if has_import or (probe and not hard_se):
        return "A"
    if hard_se:
        return "B"
    return "C"


def scan_file(path: str) -> list:
    """扫描单个文件，返回每条静默 except 的记录。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        tree = ast.parse(src)
    except Exception:
        return []
    lines = src.splitlines()
    rel = _rel(path)
    out = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def _enter_func(self, node):
            self.stack.append(getattr(node, "name", "<lambda>"))

        def _exit_func(self, node):
            self.stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = None  # 见下方赋值

        def visit_Try(self, node):
            for h in node.handlers:
                if _is_silent(h):
                    try_body = node.body
                    bound = bool(h.name)
                    etype = (ast.unparse(h.type) if h.type is not None else "裸 except")
                    out.append({
                        "file": rel,
                        "line": h.lineno,
                        "func": ".".join(self.stack) or "<module>",
                        "except_type": etype,
                        "bound": bound,
                        "bucket": _bucket(h, try_body),
                        "snippet": _first_stmt_snippet(try_body, lines),
                        "body_kind": ("pass" if isinstance(h.body[-1], ast.Pass) else
                                      "continue" if isinstance(h.body[-1], ast.Continue) else
                                      "ellipsis"),
                    })
            self.generic_visit(node)

    class _FuncVisitor(ast.NodeVisitor):
        """带作用域栈的遍历（复合类/函数名，便于定位）。"""

        def __init__(self):
            self.stack = []
            self.hits = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Try(self, node):
            for h in node.handlers:
                if _is_silent(h):
                    out.append({
                        "file": rel,
                        "line": h.lineno,
                        "func": ".".join(self.stack) or "<module>",
                        "except_type": (ast.unparse(h.type) if h.type is not None else "except:"),
                        "bound": bool(h.name),
                        "bucket": _bucket(h, node.body),
                        "snippet": _first_stmt_snippet(node.body, lines),
                        "body_kind": ("pass" if h.body and isinstance(h.body[-1], ast.Pass)
                                      else "continue" if h.body and isinstance(h.body[-1], ast.Continue)
                                      else "ellipsis"),
                    })
            self.generic_visit(node)

    _FuncVisitor().visit(tree)
    return out


def scan_all(roots=None) -> list:
    recs = []
    targets = roots or [REPO]
    for root in targets:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                recs.extend(scan_file(p))
    return recs


# ---------------------------------------------------------------- 报告

def summarize(recs: list) -> dict:
    per_file = {}
    for r in recs:
        d = per_file.setdefault(r["file"], {"A": 0, "B": 0, "C": 0, "total": 0})
        d[r["bucket"]] += 1
        d["total"] += 1
    return per_file


def count_quiet_calls() -> dict:
    """统计各文件里已完成的留痕调用数（degrade / attention），用于展示改造进度。"""
    out = {}
    pat = re.compile(r"\b(degrade|attention)\(")
    for root in (os.path.join(REPO, "bridge"), os.path.join(REPO, "libs", "qq_bot_runtime"),
                 os.path.join(REPO, "tests")):
        if not os.path.isdir(root):
            continue
        for p in iter_py(os.path.relpath(root, REPO)):
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    src = f.read()
            except Exception:
                continue
            n = len(pat.findall(src))
            if n:
                out[_rel(p)] = n
    return out


def render_md(recs: list) -> str:
    per_file = summarize(recs)
    app = sum(v["total"] for k, v in per_file.items()
              if k.startswith(("bridge/", "tests/")) or k in ("app.py", "server.py"))
    eng = sum(v["total"] for k, v in per_file.items()
              if k.startswith("libs/qq_bot_runtime/"))
    other = sum(v["total"] for k, v in per_file.items()) - app - eng
    tot = {"A": 0, "B": 0, "C": 0}
    for v in per_file.values():
        for k in tot:
            tot[k] += v[k]

    L = []
    L.append("# 静默异常改造清单（DEGRADE_AUDIT）\n")
    L.append("> 由 `tests/audit_silent_except.py` 生成，配合 `libs/qq_bot_runtime/quiet.py` 使用。\n")
    L.append("「静默异常」指 `except ...: pass / continue / ...` —— 异常被吞掉且**事后零痕迹**，"
             "出问题时无法定位。改造目标不是把它们改成抛错（多数是刻意的降级路径，抛错会打断正常流程），"
             "而是**可信地留痕**：刻意降级用 `quiet.degrade()` 计数（默认不打印，避免刷屏），"
             "真问题用 `quiet.attention()` 输出告警。\n")
    L.append("## 统计\n")
    L.append("| 范围 | 数量 |\n|---|---|")
    L.append("| App 层（`bridge/`、`app.py`、`server.py`） | %d |" % app)
    L.append("| 引擎层（`libs/qq_bot_runtime/`） | %d |" % eng)
    if other:
        L.append("| 其它 | %d |" % other)
    L.append("| **合计** | **%d** |" % len(recs))
    L.append("")
    L.append("| 分桶 | 含义 | 数量 |\n|---|---|---|")
    L.append("| A | 探测 / 默认值回落（大概率刻意） | %d |" % tot["A"])
    L.append("| B | 静默失败风险（含写/发/删等副作用） | %d |" % tot["B"])
    L.append("| C | 需人工判定 | %d |" % tot["C"])
    L.append("")

    L.append("## 按文件分布（降序）\n")
    done = count_quiet_calls()
    L.append("「已留痕」= 该文件里已写入的 `degrade()/attention()` 调用数（改造进度）。\n")
    L.append("| 文件 | A | B | C | 剩余 | 已留痕 |\n|---|---|---|---|---|---|")
    for k, v in sorted(per_file.items(), key=lambda kv: -kv[1]["total"]):
        L.append("| `%s` | %d | %d | %d | %d | %d |"
                 % (k, v["A"], v["B"], v["C"], v["total"], done.get(k, 0)))
    L.append("")
    if done:
        L.append("本轮已完成留痕的文件：%s\n" % "、".join(
            "`%s`(%d)" % (k, n) for k, n in sorted(done.items(), key=lambda x: -x[1])))

    L.append("## 逐条清单\n")
    for bucket, title in (("B", "B 静默失败风险（优先人工判定）"),
                          ("A", "A 探测 / 回落类（可用 degrade 留痕）"),
                          ("C", "C 需人工判定")):
        items = [r for r in recs if r["bucket"] == bucket]
        if not items:
            continue
        L.append("### %s（%d 条）\n" % (title, len(items)))
        L.append("| 序号 | 位置 | 函数 | except | 体 | try 首句 |\n|---|---|---|---|---|---|")
        for i, r in enumerate(sorted(items, key=lambda x: (x["file"], x["line"])), 1):
            L.append("| %d | `%s:%d` | `%s` | `%s` | %s | `%s` |"
                     % (i, r["file"], r["line"], r["func"], r["except_type"],
                        r["body_kind"], (r["snippet"] or "").replace("|", "\\|")[:60]))
        L.append("")
    L.append("---\n")
    L.append("重新生成：`python tests/audit_silent_except.py --md DEGRADE_AUDIT.md --json data/degrade_inventory.json`\n")
    return "\n".join(L)


# ---------------------------------------------------------------- 改写

_QUIET_IMPORT = "from quiet import degrade"


def _ensure_import(lines: list, tree: ast.Module) -> tuple:
    """确保顶部导入区有 `from quiet import degrade`；返回 (新行列表, 是否有改动)。"""
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "quiet":
            return lines, False
    insert_at = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            insert_at = max(insert_at, node.end_lineno or node.lineno)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            pass          # 文档串，跳过
        else:
            break         # 走到非导入语句就停（保持导入区在文件前部）
    indent = ""
    lines = list(lines)
    lines.insert(insert_at, _QUIET_IMPORT + indent)
    return lines, True


def convert_file(path: str, dry_run: bool = True, buckets=("A", "C")) -> dict:
    """把指定文件里 A/C 桶的静默 except 改写为 degrade(...) 留痕。

    行尾与文件末尾换行必须原样保留：源文件多为 CRLF，若写出 LF 会造成
    「整文件行尾混用 + git 全文件 diff」，也会丢掉 EOF 换行。
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        src = f.read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {"ok": False, "error": "语法错误，跳过：%r" % e}
    nl = "\r\n" if "\r\n" in src else "\n"
    had_final_nl = src.endswith(("\n", "\r"))
    lines = src.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if had_final_nl and lines and lines[-1] == "":
        lines.pop()                      # 去掉末尾空元素，写出时统一补回
    rel = _rel(path)

    edits = []          # (lineno, end_lineno, 新文本)
    changed = 0

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_Try(self, node):
            nonlocal changed
            for h in node.handlers:
                if not _is_silent(h):
                    continue
                b = _bucket(h, node.body)
                if b not in buckets:
                    continue
                body_last = h.body[-1] if h.body else None
                kind = ("pass" if isinstance(body_last, ast.Pass) else
                        "continue" if isinstance(body_last, ast.Continue) else "ellipsis")
                if kind == "ellipsis":
                    continue                    # `...` 极少见，留人工
                func = ".".join(self.stack) or "<module>"
                snippet = _first_stmt_snippet(node.body, lines, 50)
                note = ("降级：%s" % snippet) if snippet else "降级（原为 %s）" % kind
                note = note.replace("\\", "\\\\").replace('"', "'")
                # handler 头必须沿用原行的缩进（丢了缩进会把 try 块的结构写坏）
                orig = lines[h.lineno - 1]
                lead = orig[:len(orig) - len(orig.lstrip())]
                trail = ""
                hash_at = orig.find("#")
                if hash_at > 0:
                    trail = "  " + orig[hash_at:].strip()
                if h.type is None:
                    head = "%sexcept Exception as e:%s" % (lead, trail)
                else:
                    etype = ast.unparse(h.type)
                    head = "%sexcept %s as %s:%s" % (lead, etype, h.name or "e", trail)
                call = ('degrade("%s:%d %s", %s, "%s")'
                        % (rel, h.lineno, func, (h.name or "e"), note))
                if kind == "continue":
                    call += "\n" + lead + "    continue"
                new_body = lead + "    " + call
                edits.append((h.lineno, h.end_lineno, head + "\n" + new_body))
                changed += 1
            self.generic_visit(node)

    V().visit(tree)
    if not changed:
        return {"ok": True, "changed": 0, "file": rel}

    # 从后往前替换，避免行号漂移
    out_lines = list(lines)
    for start, end, text in sorted(edits, key=lambda x: -x[0]):
        out_lines[start - 1:end] = text.split("\n")
    # 补导入
    text2 = "\n".join(out_lines)
    tree2 = ast.parse(text2)
    out_lines, added = _ensure_import(text2.split("\n"), tree2)
    new_src = nl.join(out_lines) + (nl if had_final_nl else "")
    try:
        ast.parse(new_src)               # 落盘前自检：改写结果必须是合法 Python
    except SyntaxError as e:
        return {"ok": False, "changed": 0, "file": rel,
                "error": "改写后语法错误，已跳过：%r" % e}
    if not dry_run:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_src)
    return {"ok": True, "changed": changed, "file": rel, "import_added": added,
            "dry_run": dry_run}


def iter_py(prefix: str):
    base = os.path.join(REPO, prefix)
    if os.path.isfile(base):
        yield base
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", default="DEGRADE_AUDIT.md")
    ap.add_argument("--json", default="")
    ap.add_argument("--convert", default="", help="按前缀改写（如 bridge）")
    ap.add_argument("--write", action="store_true", help="真正落盘（默认 dry-run）")
    ap.add_argument("--buckets", default="A", help="改写哪些桶，默认 A")
    args = ap.parse_args()

    if args.convert:
        buckets = tuple(x.strip() for x in args.buckets.split(",") if x.strip())
        total = 0
        for p in sorted(iter_py(args.convert)):
            r = convert_file(p, dry_run=not args.write, buckets=buckets)
            if r.get("changed"):
                total += r["changed"]
                print("  %-52s %d 处%s" % (r["file"], r["changed"],
                                           "" if args.write else "（dry-run）"))
        print("合计 %d 处（buckets=%s, write=%s）" % (total, ",".join(buckets), args.write))
        return 0

    recs = scan_all()
    md = render_md(recs)
    with open(os.path.join(REPO, args.md), "w", encoding="utf-8", newline="") as f:
        f.write(md)
    if args.json:
        jp = os.path.join(REPO, args.json)
        os.makedirs(os.path.dirname(jp), exist_ok=True)
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(recs, f, ensure_ascii=False, indent=1)
    print("静默 except 共 %d 处，报告：%s%s"
          % (len(recs), args.md, ("，清单：" + args.json) if args.json else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
