"""对网页界面做静态对抗审查：不跑浏览器，先把「一定白屏/一定点不动」的错误找出来。

第一性原理：页面要能用，至少得满足
  1. JS 能解析（有语法错误 = 整页白屏，所有按钮都失效）
  2. 每个 $("id") 引用的元素真的存在（写错一个 id = 那个按钮点了没反应）
  3. JS 调用的每个 /api 路径后端真的有
  4. HTML 标签闭合、没有重复 id
这些都能在浏览器之外验证，而且一旦不满足就是"完全不能用"，比后端逻辑错误更致命。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "bp2md" / "web" / "static" / "index.html"
NODE = Path(r"C:\Users\PC\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe")

# 中文 Windows 的控制台默认是 GBK，打印 ✓ 之类的符号会直接抛 UnicodeEncodeError，
# 让一个"检查界面"的脚本自己先崩掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

problems: list[str] = []
notes: list[str] = []


def bad(msg: str) -> None:
    problems.append(msg)


def ok(msg: str) -> None:
    notes.append("✓ " + msg)


src = HTML.read_text(encoding="utf-8")

# ---------- 1. 拆出 script ----------
scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
print(f"script 块：{len(scripts)} 个，合计 {sum(len(s) for s in scripts)} 字符")
if not scripts:
    bad("HTML 里没有 <script> 块——页面不会有任何交互")

js = "\n".join(scripts)

# ---------- 2. 用真正的 JS 解析器检查语法 ----------
if NODE.exists():
    tmp = ROOT / ".selftest-tmp" / "ui_check.js"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(js, encoding="utf-8")
    proc = subprocess.run([str(NODE), "--check", str(tmp)],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    if proc.returncode == 0:
        ok("JS 语法通过 node --check")
    else:
        bad(f"JS 有语法错误（会导致整页白屏、所有按钮失效）：\n{proc.stderr.strip()[:500]}")
else:
    notes.append("· 没找到 node，跳过 JS 语法检查")

# ---------- 2b. 用最小 DOM 桩在 node 里真正执行一遍 ----------
# 只查语法不够：加载阶段如果引用了不存在的函数/变量，页面照样白屏。
# 这里把脚本跑起来，看顶层执行有没有抛异常。
if NODE.exists():
    tmpdir = ROOT / ".selftest-tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)
    runner = tmpdir / "ui_run.js"
    runner.write_text(
        """
// --- 最小 DOM 桩：只为让顶层脚本跑起来 ---
function el() {
  const e = { onclick:null, onchange:null, value:"", textContent:"", innerHTML:"",
           className:"", style:{}, options:[], dataset:{}, tagName:"DIV",
           scrollTop:0, clientHeight:0, scrollHeight:0, disabled:false, checked:false };
  e.addEventListener = () => {};
  e.removeEventListener = () => {};
  e.appendChild = () => {};
  e.querySelector = () => el();
  e.querySelectorAll = () => [];
  e.classList = { add(){}, remove(){}, toggle(){}, contains(){ return false; } };
  e.setAttribute = () => {};
  return e;
}
globalThis.document = {
  getElementById: () => el(), querySelectorAll: () => [],
  createElement: () => el(), addEventListener: () => {},
};
globalThis.window = {};
globalThis.setInterval = () => 0;
globalThis.clearInterval = () => {};
globalThis.clearTimeout = () => {};
globalThis.confirm = () => false;
globalThis.alert = () => {};
globalThis.XMLHttpRequest = function () {
  this.upload = {};
  this.open = () => {}; this.setRequestHeader = () => {}; this.send = () => {};
};
globalThis.fetch = () => Promise.reject(new Error("stub: no network"));
globalThis.console = console;
const __errors = [];
process.on("uncaughtException", (e) => { __errors.push(String(e)); });
process.on("unhandledRejection", (e) => { __errors.push("unhandledRejection: " + String(e)); });
""", encoding="utf-8")
    with open(runner, "a", encoding="utf-8") as fh:
        fh.write(js)
        fh.write("\nsetTimeout(() => { "
                 "console.log('__DONE__' + JSON.stringify(__errors)); }, 50);\n")
    proc = subprocess.run([str(NODE), str(runner)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60)
    out = (proc.stdout or "") + (proc.stderr or "")
    if "__DONE__" not in out:
        bad("脚本在加载阶段就崩了（页面会白屏）：\n" + out.strip()[-600:])
    elif "__DONE__[]" not in out:
        bad("脚本执行期间有未捕获异常：\n" + out.strip()[-600:])
    else:
        ok("脚本能在最小 DOM 环境下完整加载（无未捕获异常）")
# ---------- 3. id 交叉检查 ----------
html_ids = re.findall(r'\bid="([^"]+)"', src)
id_set = set(html_ids)
dupes = {i for i in html_ids if html_ids.count(i) > 1}
if dupes:
    bad(f"HTML 里有重复 id：{sorted(dupes)}")
else:
    ok(f"HTML 里 {len(id_set)} 个 id，无重复")

refs = set(re.findall(r'\$\("([^"]+)"\)', js)) | set(re.findall(r'getElementById\("([^"]+)"\)', js))
missing = sorted(r for r in refs if r not in id_set)
if missing:
    bad(f"JS 引用了不存在的元素 id（点了会没反应或整段脚本报错）：{missing}")
else:
    ok(f"JS 引用的 {len(refs)} 个元素 id 全部存在")

# ---------- 4. data-act / /api 路径 vs 后端路由 ----------
acts = set(re.findall(r'data-act="([^"]+)"', src))
sys.path.insert(0, str(ROOT))
from bp2md.web import server as web  # noqa: E402

known_actions = {"preflight", "check", "doctor", "index", "speedtest", "run",
                 "run_local", "clean"}
unknown_acts = sorted(acts - known_actions)
if unknown_acts:
    bad(f"页面上有后端不认识的动作：{unknown_acts}")
else:
    ok(f"页面上的 {len(acts)} 个动作后端都认")

api_paths = set(re.findall(r'["\'`](/api/[a-zA-Z0-9_/]*)', js))
api_paths |= {p.rstrip("/") for p in api_paths}
ok(f"页面调用的 API 路径：{sorted(api_paths)}")

# 从 Handler 里抽出实际注册的路径前缀
handler_src = (ROOT / "bp2md" / "web" / "server.py").read_text(encoding="utf-8")
registered = set(re.findall(r'u\.path == "([^"]+)"', handler_src))
registered |= set(re.findall(r'u\.path\.startswith\("([^"]+)"\)', handler_src))
unregistered = []
for p in sorted(api_paths):
    if p in registered:
        continue
    if any(p.startswith(r) for r in registered if r.endswith("/")):
        continue
    unregistered.append(p)
if unregistered:
    bad(f"页面调用了后端没有的地址：{unregistered}（后端注册的是 {sorted(registered)}）")
else:
    ok("页面调用的 API 地址后端都存在")

# ---------- 5. HTML 基本闭合 ----------
for tag in ("html", "head", "body", "script", "style"):
    o = len(re.findall(rf"<{tag}[\s>]", src))
    c = len(re.findall(rf"</{tag}>", src))
    if o != c:
        bad(f"<{tag}> 开闭不匹配：开 {o} 个、闭 {c} 个")
ok("主要标签开闭匹配")

# ---------- 6. 页面上必须有的关键控件 ----------
for need in ("links", "saveBtn", "runBtn", "stopBtn", "log", "inspectBtn",
             "parsePreview", "pickDirBtn", "scanDeep", "scanResult", "dropZone", "fileInput",
             "pendingList", "runLocalBtn", "uploadBox",
             "workUsage", "cleanBtn", "cleanAllBtn", "cleanState"):
    if need not in id_set:
        bad(f"页面缺少关键控件：{need}")
ok("关键控件齐全（链接输入框、保存、开始、停止、日志、本地文件入口）")

# ---------- 7. 备注：确认后端也真的能返回这些动作 ----------
import inspect  # noqa: E402
start_job_src = inspect.getsource(web.Handler._start_job)
for a in known_actions:
    if f'"{a}"' not in start_job_src:
        bad(f"后端 _start_job 里没有处理动作 {a}")

print("\n" + "=" * 60)
for n in notes:
    print("  " + n)
if problems:
    print("\n发现问题：")
    for p in problems:
        print("  ✗ " + p)
else:
    print("\n页面静态审查通过。")
print("=" * 60)
sys.exit(1 if problems else 0)
