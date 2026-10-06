"""上线前预检：不花钱、不用真 Key，先把能查出来的低级错误拦掉。

和 `doctor` 的分工很清楚：

- `preflight`（本模块）：域名能不能解析、端点通不通、**OSS 的桶名和地域对不对**。
  全部用占位 Key，不会创建任何任务，因此不产生费用。
- `doctor`：用真 Key 真跑一遍转写，会花掉几分钱，验证的是"确实能用"。

一个必须说清楚的边界：**百炼（DashScope）会先校验 Key 再路由**，所以拿无效
Key 打过去，正确路径和不存在的路径都返回 401。这意味着预检**只能证明网络和
域名通，不能证明路径或参数写对了**。真正确认要靠 doctor。
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

from .config import Config

PLACEHOLDER = "sk-bpan2md-preflight-placeholder"
_TIMEOUT = 25


@dataclass
class Result:
    name: str
    level: str          # ok / warn / fail / info
    detail: str
    fix: str = ""       # 可选：直接告诉用户怎么改

    @property
    def mark(self) -> str:
        return {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}[self.level]


def _fmt_exc(exc: Exception) -> str:
    text = str(exc)
    if "NameResolution" in text or "getaddrinfo" in text or "Name or service not known" in text:
        return "域名解析失败"
    if "timed out" in text.lower() or isinstance(exc, requests.Timeout):
        return "连接超时"
    return f"{type(exc).__name__}: {text[:120]}"


# --------------------------------------------------------------------------
# OSS：这一步能真正查出配置错误
# --------------------------------------------------------------------------

def check_oss(cfg: Config) -> list[Result]:
    out: list[Result] = []

    if cfg.public_base_url:
        out.append(Result("公网托管", "info",
                          f"使用 PUBLIC_BASE_URL={cfg.public_base_url}，跳过 OSS 检查"))
        try:
            resp = requests.get(cfg.public_base_url,
                                headers={"User-Agent": "bpan2md-preflight"},
                                timeout=_TIMEOUT)
            level = "ok" if resp.status_code < 400 else "warn"
            out.append(Result("托管前缀可访问", level,
                              f"HTTP {resp.status_code}（前缀本身不通不代表文件不行，"
                              "最终以 doctor 的公网可达性检查为准）"))
        except Exception as exc:  # noqa: BLE001
            out.append(Result("托管前缀可访问", "warn", _fmt_exc(exc)))
        return out

    if not (cfg.oss_endpoint and cfg.oss_bucket):
        out.append(Result("OSS 配置", "fail", "OSS_ENDPOINT / OSS_BUCKET 没填",
                          "要么配好 OSS，要么改 ASR_BACKEND=siliconflow"))
        return out

    host = cfg.oss_endpoint.strip()
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    host = host.rstrip("/")
    url = f"https://{cfg.oss_bucket}.{host}/"

    try:
        resp = requests.get(url, headers={"User-Agent": "bpan2md-preflight"},
                            timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        msg = _fmt_exc(exc)
        fix = ""
        if "域名解析失败" in msg:
            fix = (f"检查 OSS_ENDPOINT 有没有拼错（当前 {host}）。"
                   "正确形如 oss-cn-beijing.aliyuncs.com，注意不要带桶名")
        out.append(Result("OSS 桶可达", "fail", f"{msg}（{url}）", fix))
        return out

    if resp.status_code == 200:
        out.append(Result("OSS 桶可达", "ok",
                          "桶存在且允许匿名访问（公共读），转写服务能直接拉取"))
        return out

    code, endpoint = _parse_oss_error(resp.text)
    if code == "NoSuchBucket":
        out.append(Result("OSS 桶可达", "fail",
                          f"端点格式正确，但桶 {cfg.oss_bucket} 不存在",
                          "核对 OSS_BUCKET 桶名（注意不要带 .oss-cn-xxx 后缀）"))
    elif code == "AccessDenied" and endpoint:
        out.append(Result("OSS 桶地域不符", "fail",
                          f"桶 {cfg.oss_bucket} 不在 {host}，它属于 {endpoint}",
                          f"把 OSS_ENDPOINT 改成 {endpoint}"))
    elif resp.status_code == 403:
        out.append(Result("OSS 桶可达", "ok",
                          "桶存在（当前不允许匿名列出，属正常）。"
                          "只要能上传并设成公共读即可，doctor 会做最终确认"))
    elif resp.status_code == 404:
        out.append(Result("OSS 桶可达", "fail",
                          f"HTTP 404（{code or '无错误码'}）",
                          "核对 OSS_BUCKET 与 OSS_ENDPOINT"))
    else:
        out.append(Result("OSS 桶可达", "warn",
                          f"HTTP {resp.status_code} {code or ''}".strip()))
    return out


def _parse_oss_error(body: str) -> tuple[str, str]:
    """从 OSS 的 XML 错误里取出错误码和「正确的 endpoint」。"""
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "", ""
    code = (root.findtext("Code") or "").strip()
    endpoint = (root.findtext("Endpoint") or "").strip()
    return code, endpoint


# --------------------------------------------------------------------------
# 转写端点：只能验证网络与域名
# --------------------------------------------------------------------------

def check_asr(cfg: Config) -> list[Result]:
    if cfg.asr_backend == "siliconflow":
        base = cfg.siliconflow_base.rstrip("/")
        out = [_probe_get(f"{base}/models", "硅基流动端点",
                          bearer=PLACEHOLDER, expect="Token is invalid")]
        # 硅基流动的路由独立于鉴权：正确路径返回 401，错误路径返回 404。
        # 所以这里能**真正验证路径**，不像百炼那边只能证明网络通。
        out.append(_probe_transcription_path(base))
        return out

    base = cfg.dashscope_base.rstrip("/")
    out = [_probe_post(
        f"{base}/services/audio/asr/transcription",
        "百炼 ASR 提交端点",
        headers={"Authorization": f"Bearer {PLACEHOLDER}",
                 "Content-Type": "application/json",
                 "X-DashScope-Async": "enable"},
        # 注意：故意用占位 Key。用真 Key 打这个请求会真的创建转写任务并计费。
        payload={"model": cfg.bailian_model,
                 "input": {"file_urls": ["https://example.invalid/preflight.mp3"]},
                 "parameters": {}},
        expect_code="InvalidApiKey",
    )]
    out.append(Result("路径校验", "info",
                      "百炼会先校验 Key 再路由：无效 Key 下，正确路径和不存在的路径"
                      "都返回 401，所以预检不能证明路径或参数是否正确，"
                      "只能证明网络和域名通。真正的确认要靠 doctor 用真 Key 跑一次"))
    return out


def _probe_transcription_path(base: str) -> Result:
    """用一份假音频探一下转写路径是否存在。

    401 = 路径存在（只是 Key 无效）；404 = 路径写错了。这个区分是可靠的，
    因为该网关先路由再鉴权（实测：错误路径返回 404 Not Found）。
    """
    url = f"{base}/audio/transcriptions"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {PLACEHOLDER}",
                     "User-Agent": "bpan2md-preflight"},
            files={"file": ("preflight.mp3", b"\xff\xfb\x90\x00" * 10, "audio/mpeg")},
            data={"model": "FunAudioLLM/SenseVoiceSmall"},
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return Result("转写路径", "fail", f"{_fmt_exc(exc)}（{url}）")
    if resp.status_code == 404:
        return Result("转写路径", "fail", f"HTTP 404 —— 路径不存在（{url}）",
                      "检查 SILICONFLOW_BASE 是否形如 https://api.siliconflow.cn/v1")
    if resp.status_code in (401, 403):
        return Result("转写路径", "ok",
                      "路径存在（返回鉴权错误，符合预期；该网关先路由再鉴权，"
                      "所以这个 401 能证明路径是对的）")
    if resp.status_code < 400:
        return Result("转写路径", "ok", f"HTTP {resp.status_code}")
    return Result("转写路径", "warn",
                  f"HTTP {resp.status_code}: {resp.text[:160]}")


def _probe_get(url: str, name: str, bearer: str, expect: str) -> Result:
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {bearer}",
                                          "User-Agent": "bpan2md-preflight"},
                            timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return Result(name, "fail", f"{_fmt_exc(exc)}（{url}）")
    return _classify(resp, name, expect)


def _probe_post(url: str, name: str, headers: dict, payload: dict,
                expect_code: str) -> Result:
    try:
        resp = requests.post(url, headers={**headers, "User-Agent": "bpan2md-preflight"},
                             json=payload, timeout=_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return Result(name, "fail", f"{_fmt_exc(exc)}（{url}）")
    return _classify(resp, name, expect_code)


def _classify(resp: requests.Response, name: str, expect: str) -> Result:
    body = resp.text[:200].strip()
    if expect and expect.lower() in body.lower():
        return Result(name, "ok", "域名与网络通，端点返回了预期的鉴权错误")
    if resp.status_code in (401, 403):
        return Result(name, "ok", f"端点可达（HTTP {resp.status_code}）")
    if resp.status_code < 400:
        return Result(name, "ok", f"HTTP {resp.status_code}")
    return Result(name, "warn", f"HTTP {resp.status_code}: {body}")


# --------------------------------------------------------------------------

def check_completeness(cfg: Config) -> list[Result]:
    """先看配置本身齐不齐。

    这一项曾经完全没有——于是"缺 API Key"也能看到"预检通过"，而它明明一定跑不起来。
    预检的定位是"上线前把能提前发现的错都发现"，那就没有理由跳过最基本的配置完整性。
    """
    out: list[Result] = []
    for p in cfg.check():
        # check() 里少数几条是"不影响运行，但请自行确认"，别把它们算成致命错误
        level = "warn" if "不影响运行" in p else "fail"
        fix = ""
        if "DASHSCOPE_API_KEY" in p:
            fix = ("去百炼控制台创建一个 API Key（网页「设置」里有逐步说明），"
                   "填到「百炼 API Key」后点保存")
        elif "Cookie" in p or "凭据" in p:
            fix = "按「设置」页里的步骤重新复制一次百度 Cookie"
        elif "OSS_ENDPOINT" in p:
            fix = "填好 OSS 的四项，或改用 ASR_BACKEND=siliconflow"
        elif "太长" in p:
            fix = "那个值超长了，在「设置」里重新填一个正确的，或删掉 .env 里那一行"
        out.append(Result("配置完整性", level, p, fix))
    if not out:
        out.append(Result("配置完整性", "ok", "必需项都填齐了"))
    return out


def run_preflight(cfg: Config, log=print) -> int:
    import shutil

    print("=" * 64)
    print("bpan2md 上线前预检（不花钱，不使用你的真实 Key）")
    print("=" * 64)
    t0 = time.time()
    results: list[Result] = []

    print("\n[0] 配置完整性")
    completeness = check_completeness(cfg)
    for r in completeness:
        print(f"  {r.mark} {r.name}：{r.detail}")
        if r.fix:
            print(f"      → {r.fix}")
    results += completeness

    print("\n[1] 本地依赖")
    for tool in (cfg.ffmpeg, cfg.ffprobe):
        found = shutil.which(tool)
        results.append(Result(tool, "ok" if found else "fail",
                              found or "没找到，请装 ffmpeg 并加入 PATH"))
    try:
        import requests as _r  # noqa: F401
        results.append(Result("requests", "ok", "已安装"))
    except ImportError:
        results.append(Result("requests", "fail", "缺 requests：pip install requests"))

    print("\n[2] 转写端点连通性（后端：%s）" % cfg.asr_backend)
    results += check_asr(cfg)

    print("\n[3] 对象存储配置")
    results += check_oss(cfg)

    print()
    print("-" * 64)
    for r in results:
        print(f"  {r.mark} {r.name}：{r.detail}")
        if r.fix:
            print(f"      → {r.fix}")
    print("-" * 64)

    bad = [r for r in results if r.level == "fail"]
    if bad:
        print(f"\n有 {len(bad)} 项需要先修好。")
    else:
        print("\n预检通过。注意：这只证明「配置形状、网络、桶名与地域是通的」，")
        print("**没有验证你的 Key 是否有效、上传是否放行**——预检刻意不带真 Key。")
        print("接下来请跑一次「云端链路体检」（会用真 Key 真上传并转写 20 秒测试音频，"
              "约 0.001 元）：")
        print("  网页界面：② 先测一测 → 第 3 个按钮「云端链路体检」")
        print("  命令行  ：python run.py doctor")
    print(f"\n耗时 {time.time() - t0:.1f}s")
    return 1 if bad else 0
