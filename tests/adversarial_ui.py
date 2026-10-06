"""对抗式端到端演练：按浏览器的真实操作顺序驱动网页后端，并故意喂坏输入。

和 selftest 里的 API 测试不同，这里关心的是**用户视角**：
点了按钮之后，到底看得到什么？出错时日志里是人话还是 traceback？
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

# 中文 Windows 的控制台默认是 GBK，打印 ✓ 之类的符号会直接抛 UnicodeEncodeError，
# 让脚本自己先崩掉（真实发生过）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {detail}" if detail and not cond else ""))


def job(action, params=None, timeout=120):
    """起一个任务并等它结束，返回 (最终快照, 全部日志文本)。"""
    r = requests.post(f"{BASE}/api/job", json={"action": action, "params": params or {}},
                      timeout=20)
    if r.status_code != 200:
        return r.json(), ""
    jid = r.json()["job_id"]
    lines, seen, deadline = [], 0, time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.3)
        s = requests.get(f"{BASE}/api/job/{jid}?from={seen}", timeout=20).json()
        lines.extend(s.get("lines") or [])
        seen = s["total"]
        if s["status"] != "running":
            return s, "\n".join(lines)
    return {"status": "timeout"}, "\n".join(lines)


print("=" * 70)
print("对抗式演练：" + BASE)
print("=" * 70)

# ---------- 0. 第一性原理：页面到底能不能打开 ----------
print("\n[0] 页面本身")
r = requests.get(BASE + "/", timeout=20)
check("首页 200", r.status_code == 200, str(r.status_code))
check("页面是完整 HTML", r.text.strip().startswith("<!doctype html>") and "</html>" in r.text)
check("中文没乱码", "百度网盘" in r.text and "�" not in r.text[:2000])
check("引用了必要的脚本", "function loadState" in r.text and "attachJob" in r.text)

# ---------- 1. 按钮：环境自检 ----------
print("\n[1] 按钮「环境自检」")
s, log = job("check")
check("任务完成", s.get("status") == "done", str(s.get("status")))
check("日志是中文人话，不是 traceback", "Traceback" not in log and ("配置" in log or "问题" in log),
      log[:200])
check("列出了当前用的模型", "paraformer-v2" in log, log[:300])

# ---------- 2. 按钮：网络与存储预检（会真的联网）----------
print("\n[2] 按钮「网络与存储预检」")
s, log = job("preflight", timeout=180)
check("任务完成", s.get("status") == "done", str(s.get("status")))
check("没崩，有结论输出", "预检" in log and len(log) > 50, log[:200])
# 这条只断言"预检对对象存储给出了明确说法"：配了就说桶和地域对不对，
# 没配就说缺哪几项。写成"OSS 还没配"是不对的——对着一个已经配好的
# 服务器跑时，那条断言会因为"日志里出现过 OSS"而假通过。
check("对对象存储给出了明确说法（配了就报结论，没配就说缺什么）",
      "OSS" in log or "跳过 OSS" in log, log[:400])
check("没有把真实 Key 发出去", "sk-" not in log or "placeholder" in log.lower())

# ---------- 3. 按钮：云端链路体检（缺 Key 时应给明确提示）----------
print("\n[3] 按钮「云端链路体检」")
s, log = job("doctor", timeout=180)
check("任务正常结束（不是卡死）", s.get("status") in ("done", "failed"), str(s.get("status")))
check("告诉用户缺什么，而不是抛异常", "配置" in log or "不完整" in log or "Key" in log,
      log[:300])
check("没有出现 Python traceback", "Traceback" not in log, log[:300])

# ---------- 4. 对抗输入：乱喂 ----------
print("\n[4] 对抗输入")
r = requests.post(BASE + "/api/job", json={"action": "run", "params": {"links": ""}}, timeout=20)
check("空链接：拒绝起任务或明确报错", r.status_code == 400 or "job_id" in r.json(),
      r.text[:120])

r = requests.post(BASE + "/api/job", json={"action": "run",
                                          "params": {"links": "这不是链接"}}, timeout=20)
if r.status_code == 200:
    time.sleep(2)
    jid = r.json()["job_id"]
    s = requests.get(f"{BASE}/api/job/{jid}?from=0", timeout=20).json()
    log = "\n".join(s.get("lines") or [])
    check("乱链接：日志说清楚原因", "Cookie" in log or "凭据" in log or "百度" in log,
          log[:250])
    check("乱链接：没有 traceback 刷屏", "Traceback" not in log, log[:200])

r = requests.post(BASE + "/api/job", data="这不是 json", timeout=20,
                  headers={"Content-Type": "application/json"})
check("非法 JSON 体：返回 400 而不是 500", r.status_code == 400, str(r.status_code))

r = requests.post(BASE + "/api/config", json={"BAIDU_COOKIE": "x" * 3_000_000}, timeout=60)
check("超大请求体：被拒绝而不是吃满内存", r.status_code in (400, 413),
      str(r.status_code))

r = requests.post(BASE + "/api/config", json=["不是字典"], timeout=20)
check("配置体类型不对：不崩", r.status_code in (200, 400), str(r.status_code))

r = requests.get(BASE + "/api/job/abc", timeout=20)
check("job id 非数字：不返回 500", r.status_code in (200, 400, 404), str(r.status_code))

r = requests.get(BASE + "/api/file", timeout=20)
check("缺 name 参数：404 而不是 500", r.status_code == 404, str(r.status_code))

r = requests.get(BASE + "/api/file?name=" + "..%2f" * 6 + "windows%2fwin.ini", timeout=20)
check("深度路径穿越：被拒", r.status_code == 404, str(r.status_code))

r = requests.get(BASE + "/api/file?name=C:%5CWindows%5Cwin.ini", timeout=20)
check("绝对路径：被拒", r.status_code == 404, str(r.status_code))

# ---------- 5. 并发：不许同时跑两个 ----------
print("\n[5] 并发保护")
r1 = requests.post(BASE + "/api/job", json={"action": "preflight", "params": {}}, timeout=20)
r2 = requests.post(BASE + "/api/job", json={"action": "preflight", "params": {}}, timeout=20)
check("同时起两个任务：第二个被拒绝", r2.status_code == 400,
      f"{r1.status_code}/{r2.status_code} {r2.text[:100]}")
if r2.status_code == 400:
    check("拒绝理由是人话", "已经有一个任务" in r2.text, r2.text[:120])

# ---------- 6. 停止 ----------
print("\n[6] 停止")
r = requests.post(BASE + "/api/stop", json={}, timeout=20)
check("停止有响应", r.status_code == 200, str(r.status_code))
time.sleep(3)
for _ in range(40):
    s = requests.get(f"{BASE}/api/job/0?from=0", timeout=20).json()
    if s["status"] != "running":
        break
    time.sleep(0.5)
check("停止后任务确实停了", s["status"] in ("cancelled", "done", "failed"), s["status"])
check("停止后能再起新任务",
      requests.post(BASE + "/api/job", json={"action": "index", "params": {}},
                    timeout=20).status_code == 200)

# ---------- 7. 配置往返（含中文和特殊字符）----------
print("\n[7] 配置保存往返")
r = requests.post(BASE + "/api/config",
                  json={"BAIDU_TRANSFER_DIR": "/中文 目录/测试",
                        "OSS_ENDPOINT": "oss-cn-beijing.aliyuncs.com"}, timeout=20)
check("保存成功", r.status_code == 200, r.text[:120])
st = requests.get(BASE + "/api/state", timeout=20).json()
check("中文值往返正确",
      st["config"]["plain"]["BAIDU_TRANSFER_DIR"] == "/中文 目录/测试",
      st["config"]["plain"]["BAIDU_TRANSFER_DIR"])

print("\n" + "=" * 70)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
print("=" * 70)
sys.exit(1 if FAIL else 0)
