"""百度网盘：Cookie 处理、分享解析、转存、断点续传下载。

底层复用 vendor/baidu_pan 里的 MIT 脚本（skyzhao1223/baidu-pan-skill）——
它的下载通道和续传逻辑是实测过的，比自己重新逆向可靠得多。
这里直接调用它的函数，不走子进程，避免管道相关的平台问题。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

from .config import VENDOR, Config

if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

import bdpan_common  # noqa: E402
import bdpan_download  # noqa: E402
import bdpan_share  # noqa: E402


class BaiduError(RuntimeError):
    pass


class BaiduStopBatch(BaiduError):
    """不该继续处理剩下文件的错误。

    登录态失效、账号被风控、要求验证码——这些情况下剩下的文件**必然**
    也会失败。继续跑只会刷一屏失败、让人以为"这批文件都有问题"，
    还会白白占用几小时。所以这类错误要立刻中止整个批量。
    """


class BaiduAuthError(BaiduStopBatch):
    """登录态失效：换一份 Cookie 再重跑就能续上。"""


class BaiduRiskError(BaiduStopBatch):
    """触发百度风控：需要人工介入（先在网页端正常操作一次）。"""


# 下载器把「凭证被拒」包成了普通 OSError，所以只能按文案识别
_AUTH_MARKERS = ("credentials rejected", "31045", "31064", "31066",
                 "auth fail", "user not exists", "errno -6",
                 "cookies missing", "cookies.json missing")
_RISK_MARKERS = ("errno 132", "errno\": 132", "9013", "black uid",
                 "captcha", "验证码")

_AUTH_ADVICE = (
    "百度登录态已失效。\n"
    "→ 重新从浏览器复制 Cookie 更新 .env 的 BAIDU_COOKIE，"
    "删掉 work/cookies.json，然后重跑同一条命令即可续上已下载的进度。"
)
_RISK_ADVICE = (
    "触发了百度风控。\n"
    "→ 先用浏览器正常打开一次网盘（必要时过验证码），等一段时间再跑；"
    "短时间高频操作容易反复触发。"
)


def classify_error(exc: Exception) -> Exception:
    """把底层异常翻译成带明确处置建议的类型；认不出来就原样返回。"""
    text = str(exc)
    low = text.lower()
    if any(m in low for m in _AUTH_MARKERS):
        return BaiduAuthError(f"{text}\n{_AUTH_ADVICE}")
    if any(m in low for m in _RISK_MARKERS):
        return BaiduRiskError(f"{text}\n{_RISK_ADVICE}")
    return exc


# --------------------------------------------------------------------------
# Cookie
# --------------------------------------------------------------------------

def parse_cookie_header(raw: str) -> dict[str, str]:
    """把从浏览器 DevTools 复制的一整条 Cookie 字符串解析成 dict。

    支持 "a=1; b=2" 和 "a=1;b=2" 两种写法，也容忍换行。
    """
    out: dict[str, str] = {}
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key:
            out[key] = value
    return out


def ensure_cookies(cfg: Config) -> dict[str, str]:
    """保证 work/cookies.json 存在且可用，返回过滤后的 cookie 字典。

    百度只需要十来个 cookie，把浏览器整个 jar 发过去会撑爆 nginx 的
    header 缓冲（400 Request Header Or Cookie Too Large），所以这里用
    上游的 SEND_COOKIES 白名单过滤。
    """
    path = cfg.cookies_json
    cookies: dict[str, str] = {}

    if path.exists():
        try:
            cookies = bdpan_common.load_cookies(path)
        except Exception as exc:  # 损坏的文件就重写
            print(f"[baidu] 读取 {path} 失败（{exc}），将用 .env 的 BAIDU_COOKIE 重建")

    missing = bdpan_common.check_cookies(cookies) if cookies else list(bdpan_common.REQUIRED_COOKIES)

    if missing and cfg.baidu_cookie:
        raw = parse_cookie_header(cfg.baidu_cookie)
        cookies = bdpan_common.filter_cookies(raw)
        missing = bdpan_common.check_cookies(cookies)
        if not missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cookies, ensure_ascii=False, indent=1), encoding="utf-8")
            try:  # 收紧权限：这是账号凭据
                path.chmod(0o600)
            except OSError:
                pass
            print(f"[baidu] 已从 BAIDU_COOKIE 生成 {path}（用完记得删）")

    if missing:
        raise BaiduAuthError(
            "百度 Cookie 不完整，缺少: " + ", ".join(missing) + "\n"
            "获取方法：浏览器登录 pan.baidu.com → F12 → Network → 刷新 →\n"
            "任选一个请求 → Request Headers → 复制整条 Cookie → 粘到 .env 的 BAIDU_COOKIE。"
        )
    return cookies


# --------------------------------------------------------------------------
# 分享解析 / 转存
# --------------------------------------------------------------------------

def _split_url_pwd(url: str, pwd: str | None) -> tuple[str, str | None]:
    surl, url_pwd = bdpan_common.parse_share_url(url)
    return surl, (pwd or url_pwd)


def inspect_share(cfg: Config, url: str, pwd: str | None = None) -> dict:
    """列出一个分享链接里的文件。返回 {"surl","shareid","files":[...]}。"""
    ensure_cookies(cfg)
    surl, pwd = _split_url_pwd(url, pwd)
    if not pwd:
        raise BaiduError("这个分享需要提取码：请在链接里带 ?pwd=xxxx，或用 --pwd 指定")

    args = argparse.Namespace(cookies=str(cfg.cookies_json), url=url, pwd=pwd, json=True)
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            bdpan_share.cmd_inspect(args)
    except Exception as exc:
        raise _as_baidu_error(exc) from exc
    return json.loads(buf.getvalue())


def save_share(cfg: Config, url: str, pwd: str | None = None,
               dest: str | None = None, fsids: str | None = None) -> dict:
    """把分享里的文件转存到自己的网盘（下载通道只认自己网盘里的路径）。"""
    ensure_cookies(cfg)
    _surl, pwd = _split_url_pwd(url, pwd)
    if not pwd:
        raise BaiduError("这个分享需要提取码：请在链接里带 ?pwd=xxxx，或用 --pwd 指定")

    args = argparse.Namespace(
        cookies=str(cfg.cookies_json), url=url, pwd=pwd,
        dest=dest or cfg.transfer_dir, fsids=fsids,
    )
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            bdpan_share.cmd_save(args)
    except Exception as exc:
        raise _as_baidu_error(exc) from exc
    return json.loads(buf.getvalue())


def _as_baidu_error(exc: Exception) -> BaiduError:
    """把底层异常统一成 BaiduError（能识别的升级成需中止批量的类型）。"""
    classified = classify_error(exc)
    if isinstance(classified, BaiduError):
        return classified
    if isinstance(exc, bdpan_common.BaiduError):
        return BaiduError(_friendly_baidu_error(exc))
    if isinstance(exc, SystemExit):
        # cmd_* 会用 SystemExit 抛参数类错误
        return BaiduError(str(exc))
    return BaiduError(f"{type(exc).__name__}: {exc}")


def _friendly_baidu_error(exc: Exception) -> str:
    text = str(exc)
    hint = {
        "-6": "Cookie 已失效，请重新复制 BAIDU_COOKIE",
        "-9": "路径不存在或已被风控",
        "132": "触发百度风控（errno 132）。稍后再试，或先在网页端手动操作一次",
        "9013": "账号被标记异常（hit black uid file），请先用浏览器正常登录一次网盘",
        "captcha": "百度要求验证码：用浏览器打开一次这个分享链接，再重试",
        "31045": "登录态无效（31045），请重新复制 Cookie",
    }
    for key, msg in hint.items():
        if key in text:
            return f"{text}\n→ {msg}"
    return text


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

def download_saved(cfg: Config, saved: list[dict], out_dir: Path,
                   log=print) -> list[Path]:
    """把 save_share 返回的 saved 列表下载到本地（分块、断点续传）。"""
    cookies = ensure_cookies(cfg)
    fetch = bdpan_download.make_fetcher(cookies)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[Path] = []
    for item in saved:
        remote = item.get("path")
        if not remote:
            continue
        name = item.get("name") or remote.rsplit("/", 1)[-1]
        size = item.get("size") or None
        try:
            local = bdpan_download.download_one(
                fetch, remote, out_dir, name,
                size=int(size) if size else None,
                cookies=cookies if not size else None,
                relpath=item.get("relpath") or name,
                log=log,
            )
            results.append(local)
        except Exception as exc:
            # 下载器把「凭证被拒」包成了 OSError，这里统一翻译成可处置的类型
            classified = classify_error(exc)
            if classified is not exc:
                raise classified from exc
            if isinstance(exc, bdpan_common.BaiduError):
                raise BaiduError(_friendly_baidu_error(exc)) from exc
            raise
    return results


def measure_speed(cfg: Config, saved: list[dict], sample_chunks: int = 4,
                  chunk_bytes: int | None = None, log=print) -> dict:
    """测速：只抓若干块，算出这个账号当前的真实 KB/s 和 ETA。

    百度非会员限速是「账号级聚合」的，多线程不会更快，所以样本不用大。
    """
    cookies = ensure_cookies(cfg)
    target = max(saved, key=lambda x: int(x.get("size") or 0))
    remote = target.get("path")
    if not remote:
        raise BaiduError("saved 列表里没有可下载的路径")
    size = int(target.get("size") or 0) or bdpan_download.probe_size(cookies, remote)
    chunk = chunk_bytes or bdpan_download.CHUNK
    fetch = bdpan_download.make_fetcher(cookies)

    got = 0
    t0 = time.time()
    for i in range(sample_chunks):
        start = i * chunk
        if start >= size:
            break
        end = min(start + chunk - 1, size - 1)
        fetch(remote, start, end)
        got += end - start + 1
    elapsed = max(time.time() - t0, 0.001)

    bps = got / elapsed
    eta_hours = size / bps / 3600 if bps else float("inf")
    result = {
        "file": target.get("name"),
        "size": size,
        "sampled_bytes": got,
        "elapsed_sec": round(elapsed, 2),
        "kbps": round(bps / 1024, 1),
        "mbps": round(bps / 1024 / 1024, 2),
        "eta_hours_for_this_file": round(eta_hours, 2) if eta_hours != float("inf") else None,
        "verdict": _speed_verdict(bps),
    }
    log(f"[测速] {result['file']} 实测 {result['kbps']} KB/s"
        f"（{result['mbps']} MB/s），整个文件预计 {result['eta_hours_for_this_file']} 小时")
    log(f"[测速] {result['verdict']}")
    return result


def _speed_verdict(bps: float) -> str:
    kbps = bps / 1024
    if kbps >= 5 * 1024:
        return "速度很快（像是 SVIP 或已加速），整条流水线瓶颈不在下载。"
    if kbps >= 1024:
        return "速度尚可（约 1-5 MB/s），可以接受。"
    if kbps >= 300:
        return "偏慢。1GB 大约要 1 小时上下。"
    return ("很慢：这是百度非会员的账号级限速，换机器、多线程都不会变快；"
            "1GB 大约要 3-6 小时。要么开 SVIP，要么接受挂机。")
