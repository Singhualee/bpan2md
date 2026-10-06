#!/usr/bin/env python
"""用**真浏览器**（Playwright + Chromium/Edge）打开界面并操作一遍。

    python tests/browser_check.py            # 无头
    python tests/browser_check.py --headed   # 弹出真窗口，自己能看着它点

和 tests/ui_audit.py 的分工：
- ui_audit 是静态审查（JS 语法、id 引用、路由对应），秒级、零依赖；
- 这个脚本是**真环境**：真 Chromium、真 DOM、真 fetch、真文件上传。
  它能抓到静态审查永远抓不到的问题——布局把按钮盖住了、某个 id 其实选不中、
  fetch 的响应结构对不上、以及所有 JS 运行时异常。

安全：全程只做**离线**动作（页面加载、粘贴识别、扫描文件夹、上传本地文件、
只读计划）。不会连百度、不会连阿里云、不会花一分钱——用的是一份临时
.env 和临时工作目录，你真实的 .env 完全不会被读到。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".selftest-tmp"
SHOTS = TMP / "browser"
sys.path.insert(0, str(ROOT))


def find_node() -> str | None:
    cand = os.environ.get("BPAN2MD_NODE")
    if cand and Path(cand).exists():
        return cand
    found = shutil.which("node")
    if found:
        return found
    hard = Path(r"C:\Users\PC\.dsh\dsh-runtimes\dsh-primary-runtime"
                r"\dependencies\node\bin\node.exe")
    return str(hard) if hard.exists() else None


def find_playwright() -> Path | None:
    """找全局装的 playwright 的 ESM 入口。"""
    cand = os.environ.get("BPAN2MD_PLAYWRIGHT")
    if cand and Path(cand).exists():
        return Path(cand)
    appdata = os.environ.get("APPDATA")
    if appdata:
        p = Path(appdata) / "npm" / "node_modules" / "playwright" / "index.mjs"
        if p.exists():
            return p
    return None


JS = r"""
import { chromium } from "file:///__PW__";
const [base, dir, video, shotDir] = process.argv.slice(2);
const errors = [];
const notes = [];
const fail = (m) => { errors.push(m); console.log("  ✗ " + m); };
const ok = (m) => { notes.push(m); console.log("  ✓ " + m); };

const browser = await chromium.launch({ channel: process.env.PW_CHANNEL || "chromium" });
const ctx = await browser.newContext({ viewport: { width: 1280, height: 1000 },
                                       deviceScaleFactor: 1 });
const page = await ctx.newPage();
page.on("pageerror", e => fail("页面抛异常：" + e.message));
page.on("console", m => { if (m.type() === "error") fail("console.error：" + m.text()); });
page.on("requestfailed", r => {
  const t = (r.failure() || {}).errorText || "";
  fail("请求失败：" + r.url() + " " + t);
});
async function shot(name, selector) {
  const target = selector ? page.locator(selector) : page;
  await target.screenshot({ path: shotDir + "/" + name + ".png" })
    .catch(e => fail("截图 " + name + " 失败：" + e.message));
}

// ---------- 1. 打开页面 ----------
await page.goto(base, { waitUntil: "load" });
ok("页面能打开：" + await page.title());
try {
  await page.waitForFunction(() => document.querySelector("#badges").innerHTML.length > 0,
                             null, { timeout: 10000 });
  ok("状态徽章渲染出来了（loadState 成功）");
} catch (e) {
  fail("徽章一直是空的：#badges 没被填充，很可能 loadState 抛了异常");
}
await shot("1-设置");

// ---------- 2. 整段粘贴识别（在「开始转写」标签页里）----------
await page.click('nav button[data-tab="run"]');
await page.waitForTimeout(200);
const BLOB = "通过网盘分享的文件：《戎震谈男性成长》02：没有人会可怜你，"
  + "你是弱者，就注定在底层徘徊，这是自然界的法则.mp4\n"
  + "链接: https://pan.baidu.com/s/1PwCbt0-aE-al_EKVcIPs1Q?pwd=8eb6 提取码: 8eb6 "
  + "复制这段内容后打开百度网盘手机App，操作更方便哦";
await page.fill("#links", BLOB);
try {
  await page.waitForFunction(
    () => /认出|没认出/.test(document.querySelector("#parsePreview").textContent),
    null, { timeout: 10000 });
} catch (e) {
  fail("粘贴后识别预览一直没更新");
}
const pv = await page.textContent("#parsePreview");
if (/认出 1 个/.test(pv)) ok("认出了 1 个分享链接");
else fail("识别结果不对：" + pv.replace(/\s+/g, " ").slice(0, 200));
if (/8eb6/.test(pv)) ok("自动认出了提取码 8eb6");
else fail("提取码没被认出来：" + pv.replace(/\s+/g, " ").slice(0, 200));
if (/.mp4/.test(pv)) ok("认出了文件名（用于显示）");
else fail("文件名没被认出来：" + pv.replace(/\s+/g, " ").slice(0, 200));
await shot("2-粘贴识别");

// ---------- 3. 本地文件：扫描文件夹 ----------
await page.fill("#localDir", dir);
await page.click("#scanBtn");
try {
  await page.waitForSelector("#scanResult .filelist", { timeout: 20000 });
  ok("扫描文件夹出结果了");
} catch (e) {
  fail("扫描结果没渲染出来：" + (await page.textContent("#scanResult")).slice(0, 200));
}
const scanRows = await page.$$eval("#scanResult .filelist tbody tr, #scanResult .filelist tr",
                                   rs => rs.length);
if (scanRows >= 2) ok(`扫描列出 ${scanRows - 1} 个音视频文件`);
else fail("扫描结果里没有文件行（可能没认出生成的测试视频）");
await shot("3-扫描文件夹", "#localCard");

// ---------- 4. 加进待处理清单 ----------
await page.click("#addScanned");
try {
  await page.waitForFunction(
    () => /清单里共\s*1\s*个/.test(document.querySelector("#pendingList").textContent),
    null, { timeout: 5000 });
  ok("勾选的文件进了待处理清单");
} catch (e) {
  fail("清单没更新：" + (await page.textContent("#pendingList")).slice(0, 200));
}

// ---------- 5. 真·上传一个文件 ----------
await page.setInputFiles("#fileInput", video);
try {
  await page.waitForFunction(
    () => /清单里共\s*2\s*个/.test(document.querySelector("#pendingList").textContent),
    null, { timeout: 60000 });
  ok("上传成功，并自动加进了清单（共 2 个）");
} catch (e) {
  fail("上传后清单没有变成 2 个：" + (await page.textContent("#pendingList")).slice(0, 300)
       + " ／ 上传区：" + (await page.textContent("#uploadBox")).slice(0, 300));
}
const up = await page.textContent("#uploadBox");
if (/已上传/.test(up)) ok("上传区显示了成功状态和时长");
else fail("上传区没有成功提示：" + up.slice(0, 200));
await shot("4-待处理清单", "#localCard");

// ---------- 6. 只读计划（离线、不花钱）----------
await page.click("#dryLocalBtn");
try {
  await page.waitForFunction(
    () => /完成|出错|已停止/.test(document.querySelector("#jobState").textContent),
    null, { timeout: 180000 });
  ok("「只看计划」任务跑完了");
} catch (e) {
  fail("任务一直没有结束：jobState=" + (await page.textContent("#jobState")));
}
const log = await page.textContent("#log");
if (/只看计划/.test(log)) ok("日志里出现了「只看计划」标记");
else fail("日志里没有「只看计划」标记：" + log.slice(-400));
if (!/--dry-run/.test(log)) ok("日志里没有混进命令行参数（给非程序员看的界面不该出现）");
else fail("日志里出现了 --dry-run 这种命令行写法：" + log.slice(-400));
if (/本地文件/.test(log)) ok("日志确认走的是本地文件那条路");
else fail("日志里没提到本地文件：" + log.slice(-400));
if (/Traceback|出错/.test(log)) fail("日志里有异常：" + log.slice(-600));
const stateText = await page.textContent("#localState");
if (/只看计划|完成|失败/.test(stateText)) ok("本地卡片上显示了结果汇总");
else fail("本地卡片的汇总没更新：" + stateText.slice(0, 200));
if (!/待续/.test(stateText)) ok("「只看计划」不会谎报「待续 N 个」");
else fail("只看计划的结果里出现了「待续」，容易被误解成真有任务在排队：" + stateText);
await shot("5-本地只读计划", "#localCard");

// ---------- 7. 磁盘占用与清理 ----------
await page.click('nav button[data-tab="result"]');
await page.waitForTimeout(400);
const usage = await page.textContent("#workUsage");
if (/下载的原片/.test(usage) && /抽出的音频/.test(usage)) ok("磁盘占用表渲染出来了");
else fail("磁盘占用表是空的：" + usage.slice(0, 200));
if (/进度账本（保留）/.test(usage)) ok("占用表明确标出进度账本不能删");
else fail("占用表没有标出账本：" + usage.slice(0, 300));
if (/上传的原件/.test(usage)) ok("占用表覆盖了上传的原件");
else fail("占用表漏了 uploads：" + usage.slice(0, 300));
await shot("6-磁盘占用");

await page.click("#cleanBtn");
try {
  await page.waitForFunction(
    () => /清理完成|清理失败/.test(document.querySelector("#cleanState").textContent),
    null, { timeout: 90000 });
} catch (e) {
  fail("清理任务一直没有结束：" + (await page.textContent("#cleanState")));
}
const cs = await page.textContent("#cleanState");
if (/清理完成/.test(cs)) ok("「清理中间文件」跑通了：" + cs.trim());
else fail("清理失败：" + cs.slice(0, 200));
// 注意：占用表是任务结束后异步刷新的，必须等它真的更新，不能读完就断言
// （第一版就是这么误报的：读到的是清理前的旧表）
try {
  await page.waitForFunction(
    () => /下载的原片[\s\S]{0,20}?0 个/.test(document.querySelector("#workUsage").textContent),
    null, { timeout: 15000 });
  ok("清理后占用表自动刷新了（原片归零）");
} catch (e) {
  fail("清理后占用表没归零：" + (await page.textContent("#workUsage")).slice(0, 300));
}

// 连上传原件一起清（会弹确认框；浏览器里默认是不确认的，所以这里要显式接受）
page.on("dialog", d => d.accept());
await page.click("#cleanAllBtn");
try {
  await page.waitForFunction(
    () => /上传的原件[\s\S]{0,20}?0 个/.test(document.querySelector("#workUsage").textContent),
    null, { timeout: 90000 });
  ok("「连上传的原件一起清理」也跑通了（确认框被接受，占用表已归零）");
} catch (e) {
  fail("上传的原件没有被清掉：" + (await page.textContent("#workUsage")).slice(0, 300));
}

// ---------- 8. 整页长图（把滚动容器撑开，好一次看全）----------
await page.addStyleTag({ content: "html,body{height:auto} main{overflow:visible;flex:none}" });
for (const [tab, name] of [["setup", "7-设置长图"], ["run", "8-转写页长图"],
                           ["result", "9-结果长图"]]) {
  await page.click(`nav button[data-tab="${tab}"]`);
  await page.waitForTimeout(250);
  await page.screenshot({ path: shotDir + "/" + name + ".png", fullPage: true });
}

await browser.close();
console.log("__RESULT__" + JSON.stringify({ errors, notes }));
"""


def make_video(path: Path, seconds: int = 6) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            "-shortest", str(path),
        ], check=True, capture_output=True)
        return path.exists()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 生成测试视频失败：{exc}")
        return False


def main() -> int:
    headed = "--headed" in sys.argv
    node = find_node()
    pw = find_playwright()
    if not node:
        print("找不到 node，无法驱动浏览器。设 BPAN2MD_NODE 环境变量指向 node.exe。")
        return 2
    if not pw:
        print("找不到 playwright（npm 全局装的那个）。设 BPAN2MD_PLAYWRIGHT 指向 "
              "playwright/index.mjs。")
        return 2
    print(f"node       : {node}")
    print(f"playwright : {pw}")

    SHOTS.mkdir(parents=True, exist_ok=True)
    for old in SHOTS.glob("*.png"):
        old.unlink()

    work = TMP / "br_work"
    out = TMP / "br_out"
    userdir = TMP / "br_userfiles"
    for d in (work, out, userdir):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    env_path = TMP / "br.env"
    # 全是没有真实作用的假值：这条路不会连百度，也不会连阿里云
    env_path.write_text(
        "ASR_BACKEND=bailian\n"
        "BAILIAN_MODEL=paraformer-v2\n"
        "BAILIAN_DIARIZATION=true\n"
        "DASHSCOPE_API_KEY=sk-fake-for-browser-check\n"
        "OSS_ENDPOINT=oss-cn-beijing.aliyuncs.com\n"
        "OSS_BUCKET=fake-bucket\n"
        "OSS_AK_ID=fake-ak-id\n"
        "OSS_AK_SECRET=fake-ak-secret\n", encoding="utf-8")

    video = userdir / "本地测试视频.mp4"
    upload_video = TMP / "要上传的视频.mp4"
    print("准备测试素材…")
    if not make_video(video) or not make_video(upload_video):
        return 2

    from bp2md import config as config_mod
    from bp2md.web import server as web

    saved_env = config_mod.ENV_PATH
    saved_dirs = {k: os.environ.get(k) for k in ("BPAN2MD_WORKDIR", "BPAN2MD_OUTDIR")}
    config_mod.ENV_PATH = env_path
    os.environ["BPAN2MD_WORKDIR"] = str(work)
    os.environ["BPAN2MD_OUTDIR"] = str(out)

    httpd = web.make_server(0)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # 先确认"本地文件这条路不需要百度 Cookie"这件事真的成立
    from bp2md.config import Config
    probs_local = Config.load().check(require_baidu=False)
    print(f"本地文件这条路的配置检查：{probs_local or '通过（且确实不需要百度 Cookie）'}")

    js_path = TMP / "browser_check.mjs"
    js_path.write_text(JS.replace("__PW__", str(pw).replace("\\", "/")),
                       encoding="utf-8")

    # 关键：把子进程的临时目录按到工作区里。
    # 这台机器上，"从 Python 里再启动的进程"写不了系统 TEMP
    # （Chromium 启动时要 mkdtemp 一个 profile 目录，会直接 EPERM 崩掉），
    # 而工作区是可写的。指过来之后浏览器就能正常起来。
    pw_tmp = TMP / "pw-tmp"
    pw_tmp.mkdir(parents=True, exist_ok=True)
    child_env = {**os.environ, "PW_CHANNEL": os.environ.get("PW_CHANNEL", "chromium"),
                 "TMP": str(pw_tmp), "TEMP": str(pw_tmp), "TMPDIR": str(pw_tmp)}

    print(f"\n用真浏览器打开 {base} （{'有头窗口' if headed else '无头'}）…\n")
    proc = subprocess.run(
        [node, str(js_path), base, str(userdir), str(upload_video), str(SHOTS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600, env=child_env,
    )
    out_text = (proc.stdout or "") + (proc.stderr or "")
    print(out_text.replace("__RESULT__", "").strip())

    result = {"errors": ["脚本没跑完"], "notes": []}
    for line in (proc.stdout or "").splitlines():
        if line.startswith("__RESULT__"):
            try:
                result = json.loads(line[len("__RESULT__"):])
            except json.JSONDecodeError:
                pass

    httpd.shutdown()
    httpd.server_close()
    config_mod.ENV_PATH = saved_env
    for k, v in saved_dirs.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    print("\n" + "=" * 60)
    print(f"截图目录：{SHOTS}")
    if result["errors"]:
        print(f"\n发现 {len(result['errors'])} 个问题：")
        for e in result["errors"]:
            print("  ✗ " + e)
    else:
        print("\n真实浏览器验收通过。")
    print("=" * 60)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
