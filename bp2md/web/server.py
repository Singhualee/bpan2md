"""本地网页界面。

给不写命令行的人用：双击启动脚本 → 浏览器里填配置、点按钮、看进度、下载结果。
底层调的还是同一套 `bp2md` 模块（批处理走的就是 CLI 用的 `pipeline.run_round`），
所以两边行为一致，不存在"网页版是另一套逻辑"的问题。

只用标准库起服务（`http.server`），不引入 Web 框架——依赖仍然只有 `requests` 一个。
默认只监听 127.0.0.1，别人访问不到。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import baidu, config as config_mod, links as links_mod, md, media, models
from .. import preflight
from ..config import EDITABLE_KEYS, Config, update_env_file
from ..pipeline import (Pipeline, clean_workdir, durations_from_files,
                        human_hours, human_seconds, human_size, run_round,
                        run_round_local, supervise, work_usage)

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_PORT = 8765
# 一个 Cookie 顶多几十 KB。给足余量但别让超大请求把内存吃满。
MAX_BODY = 256 * 1024
# 上传的单个文件上限。设这么大是为了"实际等于不限制"——真正的把关是磁盘空间。
MAX_UPLOAD = 200 * 1024 ** 3
UPLOAD_CHUNK = 1024 * 1024
# 只监听回环地址还不够：浏览器打开 evil.com 时，如果它的域名解析到 127.0.0.1
# （DNS rebinding），请求一样会打进来。所以下面两个头必须自己验。
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


# --------------------------------------------------------------------------
# 任务：一次只跑一个，日志边跑边吐给前端
# --------------------------------------------------------------------------

class Job:
    def __init__(self, kind: str, title: str):
        self.kind = kind
        self.title = title
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self.status = "running"          # running / done / failed / cancelled
        self.error = ""
        self.result: dict = {}
        self.started = time.time()
        self.ended: float | None = None
        self.cancel = threading.Event()

    def log(self, *args) -> None:
        text = " ".join(str(a) for a in args)
        with self._lock:
            self._lines.extend(text.splitlines() or [""])

    def snapshot(self, since: int) -> dict:
        with self._lock:
            lines = self._lines[since:]
            total = len(self._lines)
        return {
            "lines": lines,
            "total": total,
            "status": self.status,
            "error": self.error,
            "result": self.result,
            "elapsed": round((self.ended or time.time()) - self.started, 1),
            "title": self.title,
            "kind": self.kind,
        }

    def finish(self, status: str, error: str = "", result: dict | None = None) -> None:
        self.status = status
        self.error = error
        if result:
            self.result = result
        self.ended = time.time()


class Hub:
    """任务队列 + 当前任务状态。一次只跑一个任务，避免互相抢网盘和额度。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.current: Job | None = None
        self.current_id = 0

    def start(self, kind: str, title: str, work) -> Job:
        with self._lock:
            if self.current and self.current.status == "running":
                raise RuntimeError("已经有一个任务在跑了，等它结束或者先点停止")
            self.current_id += 1
            job = Job(kind, title)
            self.current = job

        def runner():
            try:
                result = work(job)
                if job.cancel.is_set():
                    job.finish("cancelled", "已被你停止", result or {})
                else:
                    job.finish("done", "", result or {})
            except KeyboardInterrupt:
                job.finish("cancelled", "已被你停止")
            except Exception as exc:  # noqa: BLE001
                job.log("")
                job.log("!! 出错了：" + f"{type(exc).__name__}: {exc}")
                for line in traceback.format_exc().splitlines()[-6:]:
                    job.log("   " + line)
                job.finish("failed", f"{type(exc).__name__}: {exc}")

        threading.Thread(target=runner, daemon=True).start()
        return job

    def busy(self) -> bool:
        return bool(self.current and self.current.status == "running")


HUB = Hub()


class BadRequest(Exception):
    """请求本身有问题（400），和服务器内部错误区分开。"""


class TooLarge(Exception):
    """请求体过大（413）。"""


# --------------------------------------------------------------------------
# 各项动作
# --------------------------------------------------------------------------

def _config_state(cfg: Config) -> dict:
    problems = cfg.check(require_baidu=False)
    red = cfg.redacted()
    spec = models.lookup(cfg.bailian_model)
    return {
        "redacted": red,
        "problems": problems,
        "backend": cfg.asr_backend,
        "model": cfg.bailian_model,
        "model_desc": models.describe(cfg.bailian_model),
        "model_diarization": bool(spec and spec.diarization),
        "diarization": cfg.bailian_diarization,
        "editable": list(EDITABLE_KEYS),
        # 非敏感字段的明文值，用来回填表单。
        # 密钥类（Cookie / API Key / AK Secret）不在这里，界面上留空即"不修改"。
        "plain": {
            "OSS_ENDPOINT": cfg.oss_endpoint,
            "OSS_BUCKET": cfg.oss_bucket,
            "OSS_AK_ID": cfg.oss_ak_id,
            "BAIDU_TRANSFER_DIR": cfg.transfer_dir,
            "ASR_BACKEND": cfg.asr_backend,
            "BAILIAN_MODEL": cfg.bailian_model,
            "SILICONFLOW_MODEL": cfg.siliconflow_model,
            "BAILIAN_DIARIZATION": "true" if cfg.bailian_diarization else "false",
            "OSS_PUBLIC_READ": "true" if cfg.oss_public_read else "false",
        },
        "outdir": str(cfg.outdir),
        "catalog": [
            {"name": s.name,
             "price": (f"{s.price_per_second*3600:.3f} 元/小时"
                       if s.price_per_second is not None else "按 token 计费"),
             "diarization": s.diarization,
             "note": s.note}
            for s in models.CATALOG.values() if s.needs_public_url
        ],
    }


def _list_outputs(cfg: Config) -> list[dict]:
    out = []
    if cfg.outdir.exists():
        for p in sorted(cfg.outdir.glob("*"), key=lambda x: x.stat().st_mtime,
                        reverse=True):
            if p.is_file():
                out.append({"name": p.name, "size": p.stat().st_size,
                            "mtime": int(p.stat().st_mtime)})
    return out


def _links_from_text(text: str) -> list[tuple[str, str]]:
    """把界面里粘的整段文本变成 (url, pwd) 列表。

    识别规则统一在 bp2md/links.py 里（命令行和网页共用），所以这里能接受
    「链接」「链接 | 提取码」「链接?pwd=xxxx」「# 注释」，也能接受直接粘
    网盘 App 复制出来的那一整段（含文件名和提取码）。
    """
    return links_mod.parse_share_text(text).pairs


def _describe_parse(text: str) -> dict:
    """把识别结果摊开给界面看：认出了几个、哪些没认出来。

    这一步存在的意义是"让用户在点开始之前就知道自己粘对了没有"——
    以前只有在跑起来之后才会因为解析失败报错。
    """
    parsed = links_mod.parse_share_text(text)
    return {
        "count": len(parsed.links),
        "summary": parsed.describe(),
        "duplicates": parsed.duplicates,
        "links": [{"url": x.url, "pwd": x.pwd, "title": x.title, "line": x.line}
                  for x in parsed.links],
        "unrecognized": parsed.unrecognized,
        "missing_pwd": [x.url for x in parsed.links if not x.pwd],
    }


# --------------------------------------------------------------------------
# 本地文件：扫描文件夹 / 接收上传
# --------------------------------------------------------------------------

def uploads_dir(cfg: Config) -> Path:
    d = cfg.workdir / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list_uploads(cfg: Config) -> list[dict]:
    d = uploads_dir(cfg)
    out = []
    for p in sorted(d.glob("*"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.is_file() and not p.name.endswith(".part"):
            out.append({"name": p.name, "path": str(p),
                        "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)})
    return out


def clean_local_path(raw: str) -> Path | None:
    """把用户粘贴的内容变成路径。

    用户会粘进来的东西五花八门：带引号的、带 `file:///` 的、前面还带着
    「路径：」几个字的。与其教育用户，不如都认出来。
    """
    s = str(raw or "").strip().strip('"').strip("'").strip()
    if not s:
        return None
    s = re.sub(r"^file:///", "", s, flags=re.I)
    # Windows 盘符路径（D:\x 或 D:/x）或 UNC 路径（\\server\share）
    m = re.search(r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n]*", s)
    if m:
        s = m.group(0).strip().strip('"').strip("'").rstrip()
    return Path(s).expanduser()


def _find_media(root: Path, deep: bool, cap: int) -> tuple[list[Path], bool]:
    """列出目录里的音视频文件（实现在 media 里，命令行和这里共用同一套）。"""
    return media.find_media(root, deep=deep, cap=cap)


def _do_scan(cfg: Config, raw_path: str, deep: bool = False,
             limit: int = 500) -> dict:
    """扫描一个本机文件夹（或单个文件），列出里面的音视频。"""
    path = clean_local_path(raw_path)
    if path is None:
        raise BadRequest("没有填文件夹路径")
    if not path.exists():
        raise BadRequest(
            f"找不到这个文件夹：{path}\n"
            f"→ 在「资源管理器」里打开那个文件夹，点一下最上面的地址栏，"
            f"把整条路径复制过来粘进输入框。"
        )
    if path.is_file():
        if not media.is_media(path):
            raise BadRequest(f"这个文件不是音视频格式（{path.suffix or '无扩展名'}）")
        files, truncated = [path], False
        root = path.parent
    else:
        root = path
        files, truncated = _find_media(path, deep, limit)

    work = cfg.workdir.resolve()
    items = []
    skipped_mine = 0
    for f in files:
        try:
            resolved = f.resolve()
            if resolved == work or work in resolved.parents:
                # 别把工具自己抽出来的音频又当成输入（扫描项目目录时会撞上）
                skipped_mine += 1
                continue
            st = f.stat()
        except OSError:
            continue
        items.append({
            "name": f.name, "path": str(f), "size": st.st_size,
            "mtime": int(st.st_mtime),
            "relpath": str(f.relative_to(root)) if f != root else f.name,
        })
    return {
        "dir": str(path), "files": items, "count": len(items),
        "total_size": sum(x["size"] for x in items),
        "truncated": truncated,
        "note": (f"（这个目录里的文件太多，只列了前 {limit} 个；"
                 f"建议一次只处理一个子文件夹）" if truncated else ""),
        "skipped_mine": skipped_mine,
    }


def _choose_local_folder() -> str:
    """打开 Windows 原生文件夹选择框，返回用户选中的目录（取消则为空）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise BadRequest("当前 Python 没有可用的文件夹选择窗口，请改用拖拽上传。") from exc
    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        return filedialog.askdirectory(title="选择包含音视频的文件夹") or ""
    finally:
        root.destroy()


def _safe_upload_name(raw: str) -> str:
    """从请求里拿到一个安全的文件名（只保留文件名本身，丢掉任何目录部分）。"""
    name = Path(unquote(str(raw or ""))).name.strip()
    # Windows 不允许的字符 + 控制字符一律替换掉
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", name).strip(" .")
    if not name:
        raise BadRequest("没拿到文件名，请重新选择文件再试一次。")
    if len(name) > 180:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:150] + dot + ext[:20]) if dot else name[:180]
    return name


def _do_run_local(cfg: Config, job: Job, params: dict) -> dict:
    """转写一批本机文件（不碰百度网盘）。"""
    paths = [p for p in (params.get("paths") or [])
             if isinstance(p, str) and p.strip()]
    if not paths:
        raise RuntimeError("没有要处理的本地文件。先扫描一个文件夹，或者把文件拖进来。")
    speakers = params.get("speakers") or "auto"
    dry = bool(params.get("dry_run"))

    # 这条路完全不碰百度，所以不该因为"没填 Cookie"被拦下——
    # 只检查云端转写要用的东西。
    problems = cfg.check(require_baidu=False)
    if problems:
        job.log("这些配置还没填好，现在跑会失败：")
        for p in problems:
            job.log("  ✗ " + p)
        job.log("→ 去「① 设置」里补齐（本地上传这条路不需要百度 Cookie）。")
        raise RuntimeError("云端转写配置不完整，见上面的清单")

    totals, failures, _aborted, _states = run_round_local(
        cfg, paths, dry_run=dry, speakers=speakers,
        log=job.log, stop_check=job.cancel.is_set)
    return _summarize_job(job, totals, failures, dry_run=dry)


def _summarize_job(job: Job, totals: dict, failures: list[str],
                   dry_run: bool = False) -> dict:
    """任务收尾的统一话术——分享链接和本地文件两条路共用。"""
    job.log("")
    job.log("=" * 60)
    if dry_run:
        # 「只看计划」下 saying「待续 N 个」会让人以为真有 N 个任务排着队，
        # 其实什么都没做——这正是这个模式要传达的相反信息。
        job.log("以上是「只看计划」的结果：只做了识别和估算，"
                "没有下载、没有上传、没有花钱。")
        job.log(f"计划处理 {totals.get('total', 0)} 个文件"
                f"（每个的花费估算写在上面各条「预计转写」里）")
        job.log("确认没问题，就去掉「只看计划」再点一次「开始转写」。")
        return {"totals": totals, "failures": failures[:50], "dry_run": True}
    job.log(f"完成 {totals.get('done', 0)} 个，失败 {totals.get('failed', 0)} 个，"
            f"待续 {totals.get('pending', 0)} 个")
    job.log(f"音频总时长 {human_seconds(totals.get('audio_seconds', 0))}")
    if failures:
        job.log("失败明细：")
        for f in failures[:20]:
            job.log("  · " + f)
    job.log("结果在「结果」标签页里。别忘了点一下「生成索引」。")
    return {"totals": totals, "failures": failures[:50]}


def _do_inspect(cfg: Config, url: str, pwd: str) -> dict:
    info = baidu.inspect_share(cfg, url, pwd or None)
    seconds, unknown = durations_from_files(info["files"])
    if cfg.asr_backend == "bailian":
        cost = models.format_cost(cfg.bailian_model, seconds, unknown) if seconds \
            else "分享里没有时长信息，无法预估"
    else:
        cost = f"{cfg.siliconflow_model} 目前免费"
    return {"share": {k: v for k, v in info.items() if k != "files"},
            "files": info["files"], "seconds": seconds, "unknown": unknown,
            "cost": cost}


def _do_run(cfg: Config, job: Job, params: dict) -> dict:
    parsed = links_mod.parse_share_text(params.get("links", ""))
    if not parsed.links:
        msg = ("没有看到百度网盘的分享链接。在网盘里点「分享」→「复制链接」，"
               "把复制出来的整段直接粘进输入框就行（一行一个）。")
        if parsed.unrecognized:
            msg += "\n这些内容没认出链接：" + " ／ ".join(parsed.unrecognized[:3])
        raise RuntimeError(msg)
    targets = parsed.pairs
    for link in parsed.links:
        if link.title:
            job.log(f"识别到：{link.title}")
        if not link.pwd:
            job.log(f"提醒：{link.url} 没带提取码，如果它是加密分享会解析失败"
                    f"（需要把「提取码: xxxx」那部分一起复制进来）")
    if parsed.unrecognized:
        job.log("注意：下面这些内容没被识别成链接，如果其中本来有分享链接，"
                "请检查是不是复制断了：")
        for line in parsed.unrecognized[:5]:
            job.log("  ？ " + line)
    only = (params.get("only") or "").strip() or None
    limit = int(params["limit"]) if params.get("limit") else None
    speakers = params.get("speakers") or "auto"
    dry = bool(params.get("dry_run"))
    hours = float(params.get("retry_hours") or 0)

    holder: dict = {}

    def one_round(cfg_now):
        totals, failures, aborted, states = run_round(
            cfg_now, targets, only=only, limit=limit, dry_run=dry,
            speakers=speakers, log=job.log, stop_check=job.cancel.is_set)
        holder.update(totals=totals, failures=failures, aborted=aborted)
        job.result = {"totals": totals, "failures": failures[:50]}
        return states

    if hours and not dry:
        job.log(f"挂机模式：最多跑 {hours:g} 小时，每隔一段时间自动重试一轮；"
                f"每轮都会重新读一次设置。")
        supervise(one_round, lambda: Config.load(),
                  hours=hours, interval_sec=10 * 60, log=job.log)
    else:
        one_round(cfg)

    totals = holder.get("totals") or {}
    failures = holder.get("failures") or []
    return _summarize_job(job, totals, failures, dry_run=dry)


def _capture(job: Job, fn) -> int:
    """跑一个"直接 print 到 stdout"的函数，把输出转进任务日志。

    preflight 和 doctor 都是写给终端看的，用 print 输出。网页界面只能拿到
    log 回调，不重定向的话日志就是一片空白——用户点完按钮什么都看不到。
    """
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn()
    for line in buf.getvalue().splitlines():
        job.log(line)
    return code if isinstance(code, int) else 0


def _do_preflight(cfg: Config, job: Job) -> dict:
    code = _capture(job, lambda: preflight.run_preflight(cfg, log=job.log))
    return {"exit_code": code}


def _do_check(cfg: Config, job: Job) -> dict:
    for k, v in cfg.redacted().items():
        job.log(f"{k}: {v}")
    problems = cfg.check()
    job.log("")
    if problems:
        job.log("发现这些问题：")
        for p in problems:
            job.log("  ✗ " + p)
    else:
        job.log("✓ 配置看起来没问题")
    import shutil
    for tool in (cfg.ffmpeg, cfg.ffprobe):
        job.log(f"  {'✓' if shutil.which(tool) else '✗'} {tool}")
    return {"problems": problems}


def _do_doctor(cfg: Config, job: Job) -> dict:
    from ..doctor import run_doctor
    code = _capture(job, lambda: run_doctor(cfg, audio=None, log=job.log))
    if not job.snapshot(0)["total"]:
        job.log("（体检没有输出，请把这条当作异常反馈）")
    return {"exit_code": code}


def _do_speedtest(cfg: Config, job: Job, url: str, pwd: str) -> dict:
    job.log("测速需要先把文件转存到你自己的网盘，这一步会占一点时间。")
    info = baidu.inspect_share(cfg, url, pwd or None)
    files = [f for f in info["files"] if not f["isdir"]]
    if not files:
        raise RuntimeError("这个分享的顶层没有文件（可能是个文件夹，先「列出文件」看看）")
    target = max(files, key=lambda f: f["size"])
    job.log(f"拿最大的那个来测：{target['name']}  {human_size(target['size'])}")
    saved = baidu.save_share(cfg, url, pwd, fsids=str(target["fs_id"]))
    items = saved.get("saved") or []
    if not items:
        raise RuntimeError("转存失败，没有拿到可下载的路径")
    result = baidu.measure_speed(cfg, items, sample_chunks=4, log=job.log)

    total = sum(f["size"] for f in info["files"])
    kbps = result.get("kbps") or 0.001
    eta_h = total / (kbps * 1024) / 3600
    job.log("")
    job.log(f"这个分享总计 {human_size(total)}，按实测 {result.get('kbps')} KB/s，"
            f"全部下载完约需 {human_hours(eta_h)}")
    if kbps < 300:
        job.log("→ 这是百度非会员的账号级限速，换机器/多线程都没用。"
                "要快只有开 SVIP，或者接受挂机跑。")
    __import__("bp2md.pipeline", fromlist=["x"]).Pipeline(cfg).record_speed(
        url, kbps)
    result["eta_hours_total"] = round(eta_h, 2)
    result["total_bytes"] = total
    return result


def _do_clean(cfg: Config, job: Job, params: dict) -> dict:
    """清理中间产物。和命令行 clean 用的是同一个函数。"""
    want_uploads = bool(params.get("uploads"))
    if want_uploads:
        up = uploads_dir(cfg)
        n = len([f for f in up.glob("*") if f.is_file()])
        if n:
            job.log(f"你选择了连 work/uploads 里那 {n} 个上传原件一起删。"
                    f"那些是你自己的文件，删了就没了（原始文件在哪你应该是知道的）。")
    result = clean_workdir(cfg, raw=True, uploads=want_uploads, log=job.log)
    job.log("")
    left = work_usage(cfg)
    for name, cn in (("raw", "下载的原片"), ("audio", "抽出的音频"),
                     ("segments", "切片"), ("uploads", "上传的原件")):
        job.log(f"  {cn}：{left[name]['files']} 个，{human_size(left[name]['bytes'])}")
    job.log(f"  进度账本（保留）：{left['state']['files']} 个")
    job.log("已经转写完的文件不会因为这次清理而被重新下载或重新计费——"
            "账本记着它们的产物在哪里。")
    return result


def _do_index(cfg: Config, job: Job) -> dict:
    index = md.build_index(cfg.outdir)
    job.log(f"已索引 {index['doc_count']} 篇，共 {index['total_chunks']} 个分块")
    job.log(f"总时长 {human_hours(index['total_duration_seconds']/3600)}，"
            f"总字数 {index['total_char_count']:,}")
    return index


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "bpan2md"

    # ---- 工具 ----
    def log_message(self, *args):
        pass

    # ---- 同源防线 ----
    # 这个服务能做两件"有后果"的事：读你本机的文件、花你的转写费。
    # 只监听 127.0.0.1 挡不住浏览器：你在任何一个网页上，那段 JS 都能向
    # http://127.0.0.1:8765 发请求（跨站请求浏览器不拦），DNS rebinding 更是
    # 能让 evil.com 直接解析到 127.0.0.1。所以 Host 和 Origin 必须自己验。
    @staticmethod
    def _host_only(value: str) -> str:
        v = str(value or "").strip().lower()
        if v.startswith("["):                 # IPv6 字面量 [::1]:8765
            return v.split("]")[0] + "]"
        return v.split(":")[0]

    def _same_origin(self) -> bool:
        if self._host_only(self.headers.get("Host", "")) not in _LOCAL_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if not origin:
            return True      # 非浏览器客户端（curl / requests）不发 Origin
        if origin == "null":
            return False
        try:
            return (urlparse(origin).hostname or "").lower() in _LOCAL_HOSTS
        except ValueError:
            return False

    def _reject_cross_origin(self) -> bool:
        """不是本机来源就直接拒绝。返回 True 表示已经回过响应了。"""
        if self._same_origin():
            return False
        self.close_connection = True
        origin = self.headers.get("Origin") or ""
        self._json({"error": "拒绝了这个请求：它来自本机之外的页面。"
                             "请用 http://127.0.0.1 打开本工具的页面"
                             + (f"（Origin: {origin[:80]}）" if origin else "")}, 403)
        return True

    def _json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str, code=200, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _drain(self, length: int, cap: int = 8 * 1024 * 1024) -> int:
        """把请求体读掉并丢弃，最多读 cap 字节。

        为什么必须读：HTTP/1.1 下如果服务端不读请求体就直接回响应，
        客户端还在往里写，就会收到 RST / ConnectionAborted——表现成
        "点了提交没反应"。读掉它（有上限，防止被大文件拖死）之后，
        413 才能正常送达。
        """
        left = min(length, cap)
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                break
            left -= len(chunk)
        return length - left

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        if length > MAX_BODY:
            self._drain(length)
            raise TooLarge(f"请求体太大（{length} 字节，上限 {MAX_BODY}）")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise BadRequest(f"请求不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise BadRequest("请求体必须是一个 JSON 对象")
        return data

    # ---- GET ----
    def do_GET(self):
        if self._reject_cross_origin():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path == "/api/state":
                cfg = Config.load()
                return self._json({
                    "config": _config_state(cfg),
                    "outputs": _list_outputs(cfg),
                    "uploads": _list_uploads(cfg),
                    "work": work_usage(cfg),
                    "job": HUB.current.snapshot(0) if HUB.current else None,
                })
            if u.path == "/api/local/scan":
                cfg = Config.load()
                deep = (q.get("deep") or ["0"])[0] not in ("0", "", "false")
                return self._json(_do_scan(cfg, (q.get("path") or [""])[0], deep=deep))
            if u.path == "/api/uploads":
                return self._json({"uploads": _list_uploads(Config.load())})
            if u.path == "/api/parse":
                return self._json(_describe_parse((q.get("text") or [""])[0]))
            if u.path.startswith("/api/job/"):
                raw_id = u.path.rsplit("/", 1)[-1]
                if not raw_id.isdigit():
                    return self._json({"error": "任务编号必须是数字"}, 400)
                since_raw = (q.get("from") or ["0"])[0]
                if not str(since_raw).isdigit():
                    return self._json({"error": "from 必须是数字"}, 400)
                since = int(since_raw)
                if not HUB.current:
                    return self._json({"lines": [], "total": 0,
                                       "status": "idle", "error": "",
                                       "result": {}, "elapsed": 0}, 200)
                return self._json(HUB.current.snapshot(since))
            if u.path == "/api/file":
                return self._serve_output(q.get("name", [""])[0],
                                          download=bool(q.get("dl")))
            if u.path == "/api/outputs":
                return self._json({"outputs": _list_outputs(Config.load())})
            return self._json({"error": "没有这个地址"}, 404)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _static(self, name: str):
        path = (STATIC_DIR / name).resolve()
        if not str(path).startswith(str(STATIC_DIR)) or not path.exists():
            return self._json({"error": "找不到页面文件"}, 404)
        ctype = {"html": "text/html; charset=utf-8",
                 "css": "text/css; charset=utf-8",
                 "js": "application/javascript; charset=utf-8"}.get(
                     path.suffix.lstrip("."), "application/octet-stream")
        # 页面是这个工具自己的界面，改动很频繁。不缓存，免得用户刷新了还是旧版。
        return self._bytes(path.read_bytes(), ctype,
                           extra={"Cache-Control": "no-store, must-revalidate"})

    def _serve_output(self, name: str, download: bool):
        if not name:
            return self._json({"error": "缺少 name 参数"}, 404)
        cfg = Config.load()
        safe = Path(name).name                    # 只允许输出目录里的单层文件名
        if not safe:
            return self._json({"error": "文件名不合法"}, 404)
        path = (cfg.outdir / safe).resolve()
        if not str(path).startswith(str(cfg.outdir.resolve())) or not path.is_file():
            return self._json({"error": "找不到这个文件"}, 404)
        ctype = ("text/markdown; charset=utf-8" if safe.endswith(".md")
                 else "application/json; charset=utf-8" if safe.endswith(".json")
                 else "text/plain; charset=utf-8")
        extra = {"Content-Disposition": f'attachment; filename="{safe}"'} if download else {}
        return self._bytes(path.read_bytes(), ctype, extra=extra)

    # ---- POST ----
    def do_POST(self):
        u = urlparse(self.path)
        if self._reject_cross_origin():
            return
        # 上传走的是流式原始请求体，不能按"小 JSON"去读（也不该有 256KB 上限）
        if u.path == "/api/upload":
            return self._upload(u)
        if u.path == "/api/upload/remove":
            return self._remove_upload()
        try:
            body = self._body()
        except TooLarge as exc:
            self.close_connection = True
            return self._json({"error": str(exc)}, 413)
        except BadRequest as exc:
            return self._json({"error": str(exc)}, 400)

        try:
            if u.path == "/api/parse":
                return self._json(_describe_parse(body.get("text", "")))

            if u.path == "/api/config":
                written = update_env_file(config_mod.ENV_PATH, body or {})
                cfg = Config.load()          # 立刻重新读，改动马上生效
                return self._json({"written": written,
                                   "config": _config_state(cfg)})

            if u.path == "/api/local/scan":
                cfg = Config.load()
                return self._json(_do_scan(cfg, body.get("path", ""),
                                           deep=bool(body.get("deep"))))

            if u.path == "/api/local/pick":
                path = _choose_local_folder()
                if not path:
                    return self._json({"cancelled": True})
                return self._json({"cancelled": False, "path": path})

            if u.path == "/api/job":
                return self._start_job(body)

            if u.path == "/api/stop":
                if HUB.current and HUB.current.status == "running":
                    HUB.current.cancel.set()
                    HUB.current.log("收到停止请求，会在当前这一步结束后停下…")
                    return self._json({"ok": True})
                return self._json({"ok": False, "error": "当前没有在跑的任务"})

            if u.path == "/api/inspect":
                cfg = Config.load()
                return self._json(_do_inspect(cfg, body.get("url", ""),
                                              body.get("pwd", "")))
            return self._json({"error": "没有这个地址"}, 404)
        except BadRequest as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

    # ---- 上传 ----
    def _upload(self, u):
        """把浏览器选中的文件**流式**写进 work/uploads。

        刻意不用 multipart/form-data：标准库解析 multipart 要 cgi 模块
        （Python 3.13 已删除），而且会把整个文件读进内存。浏览器可以直接把
        File 对象当请求体发过来（XHR 的 send(file)），Content-Length 是现成的，
        服务端只要边读边落盘——几个 GB 的文件也只占一个缓冲区。
        """
        q = parse_qs(u.query)
        raw_name = self.headers.get("X-File-Name") or (q.get("name") or [""])[0]
        try:
            name = _safe_upload_name(raw_name)
        except BadRequest as exc:
            self._drain(int(self.headers.get("Content-Length") or 0), cap=1 << 20)
            self.close_connection = True
            return self._json({"error": str(exc)}, 400)
        if not media.is_media(Path(name)):
            self._drain(int(self.headers.get("Content-Length") or 0), cap=1 << 20)
            self.close_connection = True
            return self._json(
                {"error": f"「{name}」不是音视频文件，没有上传（省得白等）"}, 400)

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self.close_connection = True
            return self._json({"error": "浏览器没有给出文件大小，无法安全接收"}, 411)
        if length > MAX_UPLOAD:
            self.close_connection = True
            return self._json(
                {"error": f"文件太大（{human_size(length)}），"
                          f"上限 {human_size(MAX_UPLOAD)}"}, 413)

        cfg = Config.load()
        dest_dir = uploads_dir(cfg)
        try:
            free = shutil.disk_usage(str(dest_dir)).free
        except OSError:
            free = None
        if free is not None and free < length * 1.05:
            self.close_connection = True
            return self._json(
                {"error": f"磁盘空间不够：这个文件 {human_size(length)}，"
                          f"而 {dest_dir.drive or dest_dir} 只剩 {human_size(free)}。"
                          f"可以先腾点空间，或者改用「扫描文件夹」的方式"
                          f"（那样不会复制文件）"}, 507)

        dest = dest_dir / name
        tmp = dest_dir / (name + ".part")
        got = 0
        try:
            with open(tmp, "wb") as fh:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(UPLOAD_CHUNK, left))
                    if not chunk:
                        raise ConnectionError(
                            f"连接断了，只收到 {human_size(got)}")
                    fh.write(chunk)
                    left -= len(chunk)
                    got += len(chunk)
        except Exception as exc:  # noqa: BLE001
            try:
                tmp.unlink()
            except OSError:
                pass
            self.close_connection = True
            return self._json({"error": f"上传中断：{exc}"}, 400)
        os.replace(tmp, dest)       # 原子替换：不会留下半截文件骗过后面的流程
        try:
            duration = media.probe_duration(cfg, dest)
        except media.MediaError:
            duration = 0.0
        return self._json({"name": name, "path": str(dest), "size": got,
                           "duration": duration,
                           "uploads": _list_uploads(cfg)})

    def _remove_upload(self):
        try:
            body = self._body()
        except (TooLarge, BadRequest) as exc:
            return self._json({"error": str(exc)}, 400)
        cfg = Config.load()
        name = _safe_upload_name(body.get("name", ""))
        target = (uploads_dir(cfg) / name).resolve()
        if target.parent != uploads_dir(cfg).resolve() or not target.is_file():
            return self._json({"error": "这个文件不在上传目录里"}, 404)
        target.unlink()
        return self._json({"removed": name, "uploads": _list_uploads(cfg)})

    def _start_job(self, body: dict):
        action = body.get("action")
        params = body.get("params") or {}
        cfg = Config.load()

        if action == "preflight":
            job = HUB.start("preflight", "上线前预检", lambda j: _do_preflight(cfg, j))
        elif action == "check":
            job = HUB.start("check", "环境自检", lambda j: _do_check(cfg, j))
        elif action == "doctor":
            job = HUB.start("doctor", "云端链路体检", lambda j: _do_doctor(cfg, j))
        elif action == "index":
            job = HUB.start("index", "生成索引", lambda j: _do_index(cfg, j))
        elif action == "clean":
            job = HUB.start("clean", "清理中间文件",
                            lambda j: _do_clean(cfg, j, params))
        elif action == "speedtest":
            job = HUB.start("speedtest", "下载测速",
                            lambda j: _do_speedtest(cfg, j, params.get("url", ""),
                                                    params.get("pwd", "")))
        elif action == "run":
            job = HUB.start("run", "开始转写", lambda j: _do_run(cfg, j, params))
        elif action == "run_local":
            job = HUB.start("run_local", "转写本地文件",
                            lambda j: _do_run_local(cfg, j, params))
        else:
            return self._json({"error": f"不认识的动作：{action}"}, 400)
        return self._json({"job_id": HUB.current_id, "title": job.title})


class LocalServer(ThreadingHTTPServer):
    """只服务本机的小 HTTP 服务。

    重写 handle_error 是因为：浏览器关掉页面、或用户中途取消上传时，
    连接会被直接掐断，标准库默认会把整个 traceback 打到控制台。
    用户看到黑窗口里冒出一堆红色堆栈，会以为工具坏了——其实什么坏事都没发生。
    """
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """只建服务不启动，方便测试（传 port=0 让系统分配空闲端口）。"""
    return LocalServer(("127.0.0.1", port), Handler)


def serve(port: int = DEFAULT_PORT, open_browser: bool = True,
          ready_event: threading.Event | None = None):
    # 端口被占（比如已经开了一个窗口）时自动往后找一个，而不是直接报错退出
    httpd = None
    last_error = None
    for candidate in [port] + [port + i for i in range(1, 11)] if port else [0]:
        try:
            httpd = make_server(candidate)
            break
        except OSError as exc:
            last_error = exc
    if httpd is None:
        print(f"启动失败：端口 {port} 附近都被占用了（{last_error}）。")
        print(f"换一个端口再试，例如：python run.py web --port 9000")
        return ""

    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print("=" * 56)
    print("  bpan2md 网页界面已启动")
    print(f"  请在浏览器打开：{url}")
    if httpd.server_address[1] != port and port:
        print(f"  （{port} 被占用，已自动改用 {httpd.server_address[1]}）")
    print("  关掉这个黑窗口就是退出。")
    print("=" * 56)
    if ready_event:
        ready_event.set()
    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)),
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
    return url
