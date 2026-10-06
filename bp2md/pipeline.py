"""流水线编排：分享链接 → 下载 → 抽音频 → 上传 → 转写 → Markdown。

每个分享链接对应一个状态文件（work/state/<hash>.json）。每一步做完就落盘，
所以中断之后重跑不会重复下载、也不会重复花钱转写。

状态文件里记了两件容易被忽略的事：
- **选择参数**（--only/--limit）。换参数重跑时会重新解析分享并合并新增文件，
  否则「先 --limit 1 试跑、再跑批量」会静默地只处理那一个文件。
- **上次实测的下载速度**，用来在开跑前给出真实的耗时预估。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import baidu, links, md, media, summary
from .asr import Segment, Transcript, make_asr
from .config import Config
from .storage import make_storage


def durations_from_files(files: list[dict]) -> tuple[float, int]:
    """从分享的文件清单里汇总时长（秒）。

    百度分享页的数据块里带 duration，所以**在下载任何东西之前**就能算出
    这批素材大概要花多少转写费。返回 (总秒数, 时长未知的条目数)。
    """
    total = 0.0
    unknown = 0
    for f in files:
        d = f.get("duration")
        if d:
            try:
                total += float(d)
                continue
            except (TypeError, ValueError):
                pass
        unknown += 1
    return total, unknown


def job_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def local_url(path: Path) -> str:
    """本地文件在状态系统里的「链接」。

    用绝对路径当身份：同一个文件每次都落到同一个状态文件（所以能续跑、
    能跳过已完成的），不同文件不会互相覆盖。
    """
    return "local://" + str(Path(path).resolve())


def local_fingerprint(path: Path) -> str:
    """内容指纹（大小 + 修改时间）。

    刻意不做全文件哈希：一个 10GB 的视频读一遍要几分钟，而这个指纹的用途
    只是"同名文件被换成了另一份内容"——大小和修改时间都没变就足够了。
    """
    st = Path(path).stat()
    return hashlib.sha1(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:12]


def parse_links_file(path: Path) -> list[tuple[str, str]]:
    """读链接清单。

    支持 "url"、"url?pwd=xxxx"、"url | pwd"、# 注释，**也支持把网盘 App
    复制出来的整段（含文件名和提取码）直接贴进去**——具体识别规则在
    bp2md/links.py 里，命令行和网页界面共用同一套。
    """
    return links.parse_share_text(
        Path(path).read_text(encoding="utf-8")).pairs


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def human_hours(h: float) -> str:
    if h < 1:
        return f"{h*60:.0f} 分钟"
    if h < 48:
        return f"{h:.1f} 小时"
    return f"{h/24:.1f} 天"


def human_seconds(seconds: float) -> str:
    """时长的人话表示。

    单元测试里那些几秒钟的素材用 human_hours 会显示成「0 分钟」——
    看起来像"没读到时长"，其实只是太短了。
    """
    sec = float(seconds or 0)
    if sec < 60:
        return f"{sec:.0f} 秒"
    if sec < 3600:
        return f"{sec/60:.1f} 分钟"
    return human_hours(sec / 3600)


class Cancelled(RuntimeError):
    """用户点了「停止」。"""


class Pipeline:
    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log
        # 可选：返回 True 表示该停了。会在文件之间和每个转写片段之前检查，
        # 所以点了停止之后最多再干完手头这一小步。
        self.stop_check = None

    def _check_stop(self) -> None:
        if self.stop_check is not None and self.stop_check():
            raise Cancelled("已按你的要求停止")

    def _add_ai_summary(self, transcript: Transcript, meta: md.DocMeta) -> None:
        """总结失败不影响转写成稿；下次 render 可再次尝试。"""
        if not self.cfg.ai_summary or not self.cfg.dashscope_api_key:
            return
        self.log(f"[总结] 正在用 {self.cfg.summary_model} 生成内容总结和大纲…")
        try:
            meta.ai_summary = summary.generate(self.cfg, transcript, meta.title)
            self.log(f"[总结] 已生成（{self.cfg.summary_model}）")
        except Exception as exc:  # 总结是增强项，不能让已完成的 ASR 失败
            self.log(f"[总结] 未生成：{type(exc).__name__}: {exc}")

    # ---------------- 状态 ----------------
    def _state_path(self, url: str) -> Path:
        return self.cfg.workdir / "state" / f"{job_id(url)}.json"

    def load_state(self, url: str) -> dict:
        path = self._state_path(url)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"url": url, "files": {}}

    def save_state(self, url: str, state: dict) -> None:
        path = self._state_path(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    # ---------------- 耗时预估 ----------------
    def record_speed(self, url: str, kbps: float) -> None:
        state = self.load_state(url)
        state["speed_kbps"] = round(kbps, 1)
        state["speed_measured_at"] = datetime.now().isoformat(timespec="seconds")
        self.save_state(url, state)

    def eta_note(self, state: dict, total_bytes: int) -> str:
        kbps = state.get("speed_kbps")
        if not kbps:
            return "（还没测过速，先跑 speedtest 才能给耗时预估）"
        seconds = total_bytes / (kbps * 1024)
        return (f"按上次实测 {kbps} KB/s 估算，下载需要约 {human_hours(seconds/3600)}"
                f"（{human_size(total_bytes)}）")

    # ---------------- 选择 / 转存 ----------------
    def _pick_targets(self, info: dict, only: str | None, limit: int | None) -> list[dict]:
        """挑出要转存的顶层条目。

        注意 --limit 数的是**顶层条目**：百度分享经常是「一个文件夹里几十个视频」，
        不转存下来就看不到里面有什么。
        """
        items = [f for f in info["files"] if f["isdir"] or media.is_media(Path(f["name"]))]
        if only:
            items = [f for f in items if only in f["name"]]
        if not items:
            raise RuntimeError(
                "这个分享里没有能识别的音视频（也不含文件夹）。"
                "用 inspect 看看里面到底是什么格式。"
            )
        if limit:
            items = items[:limit]
        return items

    def _select_and_transfer(self, state: dict, url: str, pwd: str,
                             only: str | None, limit: int | None) -> None:
        cfg = self.cfg
        self.log("\n=== 解析分享 ===")
        info = baidu.inspect_share(cfg, url, pwd or None)
        state["share"] = {k: v for k, v in info.items() if k != "files"}
        self.log(f"分享里有 {info['count']} 项，来自 {info.get('share_user') or '?'}")
        for f in info["files"]:
            kind = "目录" if f["isdir"] else "文件"
            self.log(f"  {kind} {f['name']} {human_size(f['size'])}")

        targets = self._pick_targets(info, only, limit)
        n_dirs = sum(1 for t in targets if t["isdir"])
        est = sum(t["size"] for t in targets)
        self.log(f"\n选中 {len(targets)} 个顶层条目"
                 f"（其中 {n_dirs} 个目录，目录里的内容会一并转存）"
                 f"，约 {human_size(est)}")

        # 下载之前先把转写费用摊开给你看——分享元数据里有时长，不用等下完才知道
        seconds, unknown = durations_from_files(targets)
        if cfg.asr_backend == "bailian":
            from . import models as _models
            if seconds:
                self.log(f"预计转写：{_models.format_cost(cfg.bailian_model, seconds, unknown)}")
            elif unknown:
                self.log(f"预计转写：分享里没给时长（{unknown} 项），下完才能估")
        elif seconds:
            self.log(f"预计转写：{human_hours(seconds/3600)} 音频，"
                     f"{cfg.siliconflow_model} 目前免费")

        # 转存目录按分享分开，方便事后在客户端里整目录删掉
        surl = info.get("surl") or job_id(url)
        dest = f"{cfg.transfer_dir.rstrip('/')}/{surl}"
        fsids = ",".join(str(t["fs_id"]) for t in targets)
        self.log(f"\n=== 转存到自己网盘 {dest} ===")
        self.log("提示：转存本身很快，慢的是下一步下载（百度限速是账号级的）")
        saved = baidu.save_share(cfg, url, pwd or None, dest=dest, fsids=fsids)

        # 目录分享会把整棵子树展开，这里统一按扩展名筛出音视频
        media_files = [s for s in saved.get("saved", [])
                       if media.is_media(Path(s.get("name") or ""))]
        skipped = len(saved.get("saved", [])) - len(media_files)
        if skipped > 0:
            self.log(f"  跳过 {skipped} 个非音视频文件")
        if not media_files:
            raise RuntimeError("转存成功，但里面没有音视频文件（用 inspect 确认一下）")

        # 合并进已有状态：以前下好的文件不会因为重新解析而丢进度
        existing = {s.get("path"): s for s in state.get("saved", [])}
        for s in media_files:
            existing.setdefault(s.get("path"), s)
        state["saved"] = list(existing.values())
        state["selection"] = {"only": only, "limit": limit}
        self.save_state(url, state)
        for s in state["saved"]:
            self.log(f"  待处理 {s.get('relpath') or s.get('name')}")

    # ---------------- 主流程 ----------------
    def _effective_diarization(self, speakers: str) -> bool:
        """本次运行到底要不要说话人分离。

        speakers: auto（按配置）/ single（强制关掉）/ multi（强制打开）
        对单人素材关掉分离有两个好处：输出更干净，而且单段上限不必收紧到 2 小时、
        切片更少。判定不需要预先探测——结果里本来就有 speaker_id。
        """
        if self.cfg.asr_backend != "bailian":
            return False
        if speakers == "single":
            return False
        if speakers == "multi":
            return True
        return bool(self.cfg.bailian_diarization)

    def run(self, url: str, pwd: str = "", only: str | None = None,
            limit: int | None = None, dry_run: bool = False,
            speakers: str = "auto") -> dict:
        state = self.load_state(url)
        state.setdefault("pwd", pwd)
        state.setdefault("files", {})
        self._diarization = self._effective_diarization(speakers)

        selection = {"only": only, "limit": limit}
        if "saved" not in state or state.get("selection") != selection:
            if state.get("saved") and state.get("selection") != selection:
                self.log("检测到 --only/--limit 与上次不同，重新解析分享并合并新增文件")
            self._select_and_transfer(state, url, pwd, only, limit)

        return self._run_common(state, url, dry_run)

    # ---------------- 本地文件（不经过网盘）----------------
    def run_local(self, path: Path, dry_run: bool = False,
                  speakers: str = "auto") -> dict:
        """直接处理本机上的一个音视频文件。

        这条路的存在理由很实际：百度非会员的**网页下载**被限速到几十~一百多
        KB/s，但**网盘客户端**往往能跑到 1MB/s 以上。所以"用客户端下到本地，
        再让这个工具直接读本地文件"经常比让脚本自己下载快一个数量级——
        而且完全不需要百度 Cookie，也不占网盘中转目录。
        """
        cfg = self.cfg
        path = Path(path)
        if not path.exists():
            raise RuntimeError(
                f"找不到这个文件：{path}\n"
                f"→ 如果是拖进网页上传的，重新拖一次；"
                f"如果是文件夹里的文件，把它放回原处再重跑。"
            )
        if path.is_dir():
            raise RuntimeError(
                f"这是一个文件夹，不是文件：{path}\n"
                f"→ 想处理整个文件夹里的视频，请用「扫描文件夹」把里面的文件列出来再加进清单。"
            )
        if not media.is_media(path):
            raise RuntimeError(
                f"这个文件不是音视频格式（{path.suffix or '没有扩展名'}）：{path.name}\n"
                f"支持的有：mp4/mkv/avi/mov/flv/wmv/webm/mp3/wav/m4a/aac/flac 等"
            )

        url = local_url(path)
        state = self.load_state(url)
        state["local"] = True
        state["path"] = str(path)
        state.setdefault("files", {})
        self._diarization = self._effective_diarization(speakers)

        stat = path.stat()
        name = path.name
        entry = state["files"].setdefault(name, {})
        entry.update({
            "name": name, "relpath": name, "remote": None,
            "size": stat.st_size, "local": str(path), "local_only": True,
        })

        self.log(f"\n=== 本地文件 {name}（{human_size(stat.st_size)}）===")
        self.log("这条路直接读本机文件，不经过百度网盘下载。")

        # 免费的先探一下时长：转写是按音频时长计费的，不该等抽完音频才知道价钱
        try:
            duration = media.probe_duration(cfg, path)
        except media.MediaError as exc:
            raise RuntimeError(
                f"读不出这个文件的时长，可能不是有效的音视频文件：{name}\n{exc}"
            ) from exc
        if duration:
            entry["duration"] = duration
            self.log(f"时长 {human_seconds(duration)}")
            if cfg.asr_backend == "bailian":
                from . import models as _models
                self.log("预计转写：" + _models.format_cost(
                    cfg.bailian_model, duration, 0))
            else:
                self.log(f"预计转写：{cfg.siliconflow_model} 目前免费")
        else:
            self.log("提醒：探测不到时长（文件可能不完整），仍然会试着重试")

        state["saved"] = [{
            "path": None, "name": name, "relpath": name,
            "size": stat.st_size, "local": str(path), "local_only": True,
        }]
        # 内容指纹交给 _one_file 管：它要在处理前拿**上一次记下的**指纹和现在
        # 的比对。在这里直接覆盖的话，等于每次开跑前先把证据擦掉——同名文件
        # 换了内容就永远检测不出来了（这正是"静默拿旧稿子"的那条路）。
        self.save_state(url, state)

        return self._run_common(state, url, dry_run)

    # ---------------- 两种入口共用的后半段 ----------------
    def _run_common(self, state: dict, url: str, dry_run: bool) -> dict:
        local_only = bool(state.get("local"))
        todo = [s for s in state.get("saved", [])
                if state["files"].get(self._key(s), {}).get("status") != "done"]
        total = sum(int(s.get("size") or 0) for s in todo)
        self.log(f"\n=== 本次要处理 {len(todo)} 个文件，共 {human_size(total)} ===")
        if local_only:
            self.log("（本地文件，不需要下载，所以网速和耗时都不是问题）")
        else:
            self.log(self.eta_note(state, total))

        if dry_run:
            # 措辞要同时适用于命令行和网页界面：网页上没有 --dry-run 这个说法
            self.log("\n[只看计划] 只做识别和估算，不下载、不上传、不产生费用。"
                     "真正开跑时去掉这个选项即可。")
            return state

        # 本地文件不需要留出"再下一份视频"的空间：抽出来的音频只有视频的
        # 几十分之一（32kbps ≈ 每小时 14MB），磁盘不是这条路上的风险。
        if not local_only:
            self._check_disk(total)

        for item in state["saved"]:
            self._check_stop()
            key = self._key(item)
            entry = state["files"].setdefault(key, {})
            entry["name"] = item.get("name")
            entry["relpath"] = item.get("relpath") or item.get("name")
            entry["remote"] = item.get("path")
            entry["size"] = item.get("size")
            entry["local_only"] = bool(item.get("local_only")) or bool(entry.get("local_only"))
            if item.get("local"):
                entry["local"] = item["local"]
            try:
                self._one_file(state, url, key, entry)
            except baidu.BaiduStopBatch as exc:
                # 登录态失效/风控：剩下的一定也会失败，立刻停
                entry["status"] = "failed"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                self.save_state(url, state)
                raise
            except Exception as exc:  # 单个文件失败不影响其它
                entry["status"] = "failed"
                entry["error"] = f"{type(exc).__name__}: {exc}"
                self.log(f"[{key}] 失败：{exc}")
            self.save_state(url, state)
        return state

    def _check_disk(self, need_bytes: int) -> None:
        """开跑前看一眼磁盘。批量下几十 GB 时，写到一半没空间很难受。"""
        if not need_bytes:
            return
        try:
            free = shutil.disk_usage(str(self.cfg.workdir)).free
        except OSError:
            return
        if free < need_bytes:
            raise RuntimeError(
                f"磁盘空间不足：待下载 {human_size(need_bytes)}，"
                f"可用 {human_size(free)}。清理 work/raw 后重跑。"
            )
        if free < need_bytes * 1.2:
            self.log(f"提醒：磁盘偏紧——待下载 {human_size(need_bytes)}，"
                     f"可用 {human_size(free)}（抽音频后还会再占一些）")

    @staticmethod
    def _key(item: dict) -> str:
        """用 relpath 当键：文件夹分享里不同子目录可能有同名文件。"""
        return item.get("relpath") or item.get("name") or "?"

    def _one_file(self, state: dict, url: str, key: str, entry: dict) -> None:
        cfg = self.cfg
        dia = getattr(self, "_diarization", bool(cfg.bailian_diarization))
        name = entry.get("relpath") or key
        stem = Path(name).stem

        # 本地文件被换成另一份内容时必须重做。不做这个检查的后果是静默的：
        # 音频缓存按文件名复用，于是"同名但内容不同"的新文件会直接拿到
        # 上一次的稿子——看起来成功，其实完全对不上。
        source_changed = False
        if entry.get("local_only") and entry.get("local"):
            src = Path(entry["local"])
            if src.exists():
                now_fp = local_fingerprint(src)
                old_fp = entry.get("local_fingerprint")
                if old_fp and old_fp != now_fp:
                    self.log(f"[{key}] 本地文件的内容变了（大小或修改时间不同），"
                             f"之前的结果作废，重新抽音频并重新转写")
                    self._drop_media_cache(stem, old_fp)
                    entry["local_fingerprint"] = now_fp
                    entry["audios"] = []
                    entry["results"] = []
                    entry.pop("outputs", None)
                    entry["status"] = "prepared"
                    source_changed = True
                elif not old_fp:
                    entry["local_fingerprint"] = now_fp

        # 分离设置变了就必须重转——但这一步得放在「已完成就跳过」**之前**，
        # 否则对已经跑完的文件改 --speakers 会被静默忽略。
        dia_changed = (entry.get("diarization") is not None
                       and bool(entry["diarization"]) != dia)
        if entry.get("status") == "done" and not dia_changed and not source_changed:
            if self._outputs_ok(entry):
                self.log(f"[{key}] 已完成，跳过")
                return
            # 产物被删了（比如手动清了 output/），重跑一遍而不是假装成功
            self.log(f"[{key}] 产物文件不在了，重新生成")
            entry["status"] = "prepared"
        slug = md.slugify(name)

        # 中间产物（音频/切片）按内容指纹命名，所以换了内容自然就不会命中旧缓存
        media_stem = (f"{stem}__{entry['local_fingerprint']}"
                      if entry.get("local_only") and entry.get("local_fingerprint")
                      else stem)

        # ---- 1. 拿到本地文件（本地来源跳过下载）----
        local = Path(entry["local"]) if entry.get("local") else None
        if entry.get("local_only"):
            local = self._resolve_local_source(local)
            self.log(f"[{key}] 本地文件，跳过网盘下载")
        elif local and local.exists() and self._local_ok(local, entry):
            self.log(f"[{key}] 已有完整的本地文件，跳过下载")
        else:
            self.log(f"\n=== 下载 {name} ===")
            local_dir = cfg.workdir / "raw" / slug
            files = baidu.download_saved(cfg, [{
                "path": entry["remote"], "name": entry.get("name") or name,
                "size": entry.get("size"), "relpath": name,
            }], local_dir, log=self.log)
            if not files:
                raise RuntimeError("下载没有产出文件")
            local = files[0]
            entry["local"] = str(local)
            self._verify_local(local, entry, key)

        # ---- 2. 媒体处理 ----
        results = entry.setdefault("results", [])
        audios = entry.get("audios") or []
        # 中间产物可能被 clean 删掉、或被手工清理过。缺了必须重新生成，
        # 否则会拿着一个不存在的路径去上传，报一个看不懂的 FileNotFoundError。
        stale = [i for i, p in enumerate(audios)
                 if not Path(p["path"]).exists()
                 and not (i < len(results) and Path(results[i]).exists())]
        if stale:
            self.log(f"[{key}] 音频中间产物已不在（可能跑过 clean），重新生成")
            entry["audios"] = []
            audios = []
        if not audios:
            self.log(f"\n=== 抽取音频 {name} ===")
            parts = media.prepare(cfg, local, log=self.log, stem=media_stem)
            entry["audios"] = [{"path": str(p), "offset": off} for p, off in parts]
            entry["status"] = "prepared"

        # ---- 3/4. 上传 + 转写 ----
        if dia_changed:
            # 缓存的结果是用另一套参数转出来的，不能复用
            self.log(f"[{key}] 说话人分离设置从 {entry['diarization']} 变成 {dia}，"
                     f"需要重新转写（会重新计费）")
            entry["results"] = []
            results = entry["results"]
            entry["status"] = "prepared"
        entry["diarization"] = dia

        for i, part in enumerate(entry["audios"]):
            if i < len(results) and Path(results[i]).exists():
                continue
            self._check_stop()
            audio = Path(part["path"])
            tr = self._transcribe_one(audio, url)
            if part.get("offset"):
                shift = int(float(part["offset"]) * 1000)
                for seg in tr.segments:
                    seg.start_ms += shift
                    seg.end_ms += shift
            tr.duration_ms = tr.duration_ms or int(
                max((s.end_ms for s in tr.segments), default=0))

            depth = len(entry["audios"])
            self._log_speaker_verdict(key, tr, dia, depth)
            meta = md.DocMeta(
                title=Path(name).stem + (f"（第 {i+1}/{depth} 段）" if depth > 1 else ""),
                source_file=Path(name).name,
                source_url=url,
                duration_ms=tr.duration_ms,
                asr_backend=cfg.asr_backend,
                asr_model=tr.model or (
                    cfg.bailian_model if cfg.asr_backend == "bailian"
                    else cfg.siliconflow_model),
                diarization=dia,
                language="zh",
                transcribed_at=md.now_iso(),
                parts=depth,
                extra={"relpath": name} if name != Path(name).name else None,
            )
            part_slug = slug + (f"_part{i+1:03d}" if depth > 1 else "")
            # 多段素材等合并后只总结一次，避免重复调用文本模型。
            if depth == 1:
                self._add_ai_summary(tr, meta)
            outs = md.write_outputs(tr, meta, cfg.outdir, slug=part_slug)
            results.append(str(outs["result"]))
            entry["status"] = "transcribed"
            self.log(f"[{key}] 已输出 {outs['markdown'].name}（{outs['chunk_count']} 块）")
            self.save_state(url, state)

        # ---- 5. 合并多段 ----
        if len(results) > 1:
            self.log(f"\n=== 合并 {len(results)} 段（{name}）===")
            merged = self._merge(results)
            meta = md.DocMeta(
                title=Path(name).stem, source_file=Path(name).name, source_url=url,
                duration_ms=merged.duration_ms, asr_backend=cfg.asr_backend,
                asr_model=merged.model,
                diarization=dia,
                transcribed_at=md.now_iso(),
                parts=len(results),
                extra={"relpath": name} if name != Path(name).name else None,
            )
            self._log_speaker_verdict(key, merged, dia, len(results))
            if len(results) > 1 and dia:
                self.log(f"[{key}] 注意：分段转写的发言人编号只在段内有效，"
                         f"已在正文顶部标注该限制")
            self._add_ai_summary(merged, meta)
            outs = md.write_outputs(merged, meta, cfg.outdir, slug=slug)
            entry["outputs"] = {k: str(v) for k, v in outs.items() if k != "chunk_count"}
            entry["duration_ms"] = merged.duration_ms
            self.log(f"[{key}] 合并完成 {outs['markdown'].name}（{outs['chunk_count']} 块）")
        elif results:
            # 用已保存的 result.json 重新渲染：命中缓存时也要保证产物在位，
            # 否则手动删掉 Markdown 之后重跑会「跳过」而永远补不回来。
            tr, meta = md.load_result(Path(results[0]))
            if not meta.ai_summary:
                self._add_ai_summary(tr, meta)
            outs = md.write_outputs(tr, meta, cfg.outdir, slug=slug)
            entry["outputs"] = {k: str(v) for k, v in outs.items() if k != "chunk_count"}
            entry["duration_ms"] = tr.duration_ms

        entry["status"] = "done"
        entry["finished_at"] = datetime.now().isoformat(timespec="seconds")

    # ---------------- 校验 ----------------
    @staticmethod
    def _outputs_ok(entry: dict) -> bool:
        """记录过的产物是否都还在。没有记录时按「在」处理（兼容旧状态文件）。"""
        outputs = entry.get("outputs") or {}
        if not outputs:
            return True
        return all(Path(p).exists() for p in outputs.values())

    @staticmethod
    def _resolve_local_source(local: Path | None) -> Path:
        """本地文件这条路唯一的校验：文件还在、不是空的。

        这条路**只读**用户的文件，绝不删改——源文件是用户自己的资产，
        工具没有资格动它。
        """
        if local is None:
            raise RuntimeError("状态文件里没有记录本地文件路径，请重新选一次文件")
        if not local.exists():
            raise RuntimeError(
                f"本地文件不在了：{local}\n"
                f"→ 如果是拖进网页上传的，重新拖一次；"
                f"如果是文件夹里的文件，把它放回原处再重跑。"
            )
        if local.is_dir():
            raise RuntimeError(f"预期是文件，但这是个文件夹：{local}")
        if local.stat().st_size == 0:
            raise RuntimeError(f"这个文件是空的（0 字节）：{local}")
        return local

    def _drop_media_cache(self, stem: str, fingerprint: str) -> None:
        """删掉某个指纹对应的中间产物（换了内容之后就不用留着了）。"""
        if not fingerprint:
            return
        audio = self.cfg.workdir / "audio" / f"{stem}__{fingerprint}.mp3"
        seg_dir = self.cfg.workdir / "segments" / f"{stem}__{fingerprint}"
        try:
            if audio.exists():
                audio.unlink()
            if seg_dir.exists():
                shutil.rmtree(seg_dir, ignore_errors=True)
        except OSError:
            pass          # 删不掉不影响正确性，最多多占点空间

    @staticmethod
    def _local_ok(local: Path, entry: dict) -> bool:
        """本地文件是否还算「完整」：大小对得上，且分块账本显示全部下完。"""
        expected = entry.get("size")
        if expected and local.stat().st_size != int(expected):
            return False
        state_path = Path(str(local) + ".dlstate.json")
        if state_path.exists():
            try:
                st = json.loads(state_path.read_text(encoding="utf-8"))
                if st.get("total_chunks") and len(st.get("done", [])) < st["total_chunks"]:
                    return False
            except (json.JSONDecodeError, OSError):
                return False
        return True

    def _verify_local(self, local: Path, entry: dict, key: str) -> None:
        """下载完做一次体检。

        为什么要在花钱之前做：百度下载动辄几小时，中途断掉留下一个
        长度对但内容是空洞的文件是很常见的事，拿它去转写既浪费钱又浪费时间。
        """
        expected = entry.get("size")
        actual = local.stat().st_size
        if expected and actual != int(expected):
            raise RuntimeError(
                f"下载不完整：期望 {human_size(int(expected))}，实际 {human_size(actual)}。"
                f"删掉 {local} 和它的 .dlstate.json 后重跑会重新下载。"
            )
        expect_dur = entry.get("duration")
        if expect_dur:
            try:
                got = media.probe_duration(self.cfg, local)
            except media.MediaError:
                got = 0
            if got and abs(got - float(expect_dur)) > max(5.0, float(expect_dur) * 0.05):
                self.log(f"[{key}] 警告：时长对不上（网盘记录 {expect_dur}s，"
                         f"本地 {got:.0f}s），文件可能损坏")

    # ---------------- 转写 ----------------
    def _log_speaker_verdict(self, key: str, tr: Transcript, dia: bool,
                             parts: int) -> None:
        """把「单人还是多人」这个判定说清楚——它是从结果里数出来的，不花钱。

        对知识库很实用：单人素材不需要「发言人 1：」这种前缀刷满全文。
        """
        if not dia:
            return
        n = tr.speaker_count
        if n <= 1:
            self.log(f"[{key}] 判定为单人内容（{n or '未识别到'} 个说话人），"
                     f"正文不会加「发言人」前缀（数量仍记在元数据里）")
        else:
            self.log(f"[{key}] 判定为多人内容（{n} 个说话人），正文按发言人标注")

    def _transcribe_one(self, audio: Path, url: str) -> Transcript:
        cfg = self.cfg
        asr = make_asr(cfg)
        if cfg.asr_backend == "bailian":
            key = f"{cfg.oss_prefix.strip('/')}/{audio.name}"
            storage = make_storage(cfg)
            self.log(f"\n=== 上传音频 {audio.name}（{audio.stat().st_size/1024/1024:.1f} MB）===")
            public_url = storage.upload(audio, key, log=self.log)
            self.log(f"[storage] {public_url[:100]}{'...' if len(public_url) > 100 else ''}")
            try:
                return asr.transcribe(public_url, log=self.log)
            finally:
                if cfg.oss_cleanup:
                    storage.delete(key)
                    self.log(f"[storage] 已删除 OSS 上的 {key}")
        return asr.transcribe(audio, log=self.log)

    def _merge(self, result_paths: list[str]) -> Transcript:
        segs: list[Segment] = []
        duration = 0
        model = ""
        for path in result_paths:
            tr, _meta = md.load_result(Path(path))
            segs.extend(tr.segments)
            duration = max(duration, tr.duration_ms)
            model = model or tr.model
        segs.sort(key=lambda s: s.start_ms)
        return Transcript(segments=segs, duration_ms=duration, model=model,
                          backend=self.cfg.asr_backend)


def run_round(cfg: Config, targets: list[tuple[str, str]], *,
              only: str | None = None, limit: int | None = None,
              dry_run: bool = False, speakers: str = "auto",
              log=print, stop_check=None):
    """跑一轮所有链接。返回 (统计, 失败列表, 是否被中止, 各链接状态)。

    命令行和网页界面都调这一个函数，保证两边行为完全一致。
    """
    pipe = Pipeline(cfg, log=log)
    pipe.stop_check = stop_check
    totals = {"total": 0, "done": 0, "failed": 0, "pending": 0,
              "done_bytes": 0, "audio_seconds": 0.0}
    failures: list[str] = []
    aborted = False
    states: list[dict] = []

    for i, (url, pwd) in enumerate(targets, 1):
        log(f"\n{'=' * 70}")
        log(f"[{i}/{len(targets)}] {url}")
        log(f"{'=' * 70}")
        try:
            state = pipe.run(url, pwd=pwd, only=only, limit=limit,
                             dry_run=dry_run, speakers=speakers)
        except Cancelled as exc:
            log(f"\n已停止：{exc}")
            aborted = True
            break
        except baidu.BaiduStopBatch as exc:
            log(f"\n!! 已中止整个批量：{exc}")
            totals["failed"] += 1
            failures.append(f"{url} → {exc}")
            aborted = True
            break
        except Exception as exc:  # noqa: BLE001
            totals["failed"] += 1
            failures.append(f"{url} → {type(exc).__name__}: {exc}")
            log(f"!! 这个链接失败：{type(exc).__name__}: {exc}")
            continue
        states.append(state)
        s = summarize(state)
        for k in ("total", "done", "failed", "pending", "done_bytes"):
            totals[k] += s[k]
        totals["audio_seconds"] += s["audio_seconds"]
        failures.extend(f"{url} → {f['file']}: {f['error']}" for f in s["failures"])
    return totals, failures, aborted, states


def dir_usage(path: Path) -> tuple[int, int]:
    """返回 (文件数, 总字节)，用来回答"到底是什么占了地方"。"""
    n = 0
    total = 0
    if Path(path).exists():
        for f in Path(path).rglob("*"):
            try:
                if f.is_file():
                    n += 1
                    total += f.stat().st_size
            except OSError:
                pass
    return n, total


def work_usage(cfg: Config) -> dict:
    """work/ 里每一类东西各占多少。"""
    out: dict[str, dict] = {}
    for sub in ("raw", "audio", "segments", "uploads", "state", "result"):
        n, size = dir_usage(cfg.workdir / sub)
        out[sub] = {"files": n, "bytes": size}
    ck = cfg.cookies_json
    out["cookies.json"] = {
        "files": 1 if ck.exists() else 0,
        "bytes": ck.stat().st_size if ck.exists() else 0,
    }
    return out


def clean_workdir(cfg: Config, *, raw: bool = True, uploads: bool = False,
                  log=print) -> dict:
    """删掉中间产物，把空间还回去。

    **一定要保留 `state/`**：那里记着"哪些文件已经花钱转写过了"。把它删掉，
    下次重跑会把所有文件当新的重新下载、**重新计费**。所以清理的边界是
    "能重新算出来的东西"（下下来的原片、抽出来的音频、切片），
    而不是"算出来要花钱的东西"（转写结果和进度账本）。

    唯一不在此列的是 `uploads/`（网页上传时复制进来的那份原件）——
    它是用户自己的资料，得由用户明确说了才删。
    """
    removed = 0
    freed = 0
    detail: dict[str, dict] = {}
    targets = ["audio", "segments"] + (["raw"] if raw else []) \
        + (["uploads"] if uploads else [])
    for sub in targets:
        d = cfg.workdir / sub
        n, size = dir_usage(d)
        if not d.exists():
            continue
        for f in sorted(d.rglob("*")):
            if not f.is_file():
                continue
            try:
                f.unlink()
                removed += 1
            except OSError as exc:
                log(f"删不掉 {f}：{exc}")
        # 顺手把空目录也收掉，不然 year 月 日的空壳会越积越多
        for sub_dir in sorted((x for x in d.rglob("*") if x.is_dir()),
                              key=lambda x: len(x.parts), reverse=True):
            try:
                sub_dir.rmdir()
            except OSError:
                pass
        detail[sub] = {"files": n, "bytes": size}
        freed += size
    log(f"清理了 {removed} 个中间文件，释放 {human_size(freed)}")
    if not uploads and (cfg.workdir / "uploads").exists():
        n, size = dir_usage(cfg.workdir / "uploads")
        if n:
            log(f"（上传进来的原件还留在 work/uploads，占 {human_size(size)}；"
                f"要删得单独说一声——那是你自己的文件）")
    log("进度账本 work/state 保留了：已经转写过的文件不会被重新下载或重新计费。")
    return {"removed": removed, "freed_bytes": freed, "detail": detail}


def run_round_local(cfg: Config, paths: list, *, dry_run: bool = False,
                    speakers: str = "auto", log=print, stop_check=None):
    """跑一批**本机文件**。返回值和 run_round 一样。

    和 run_round 的差别只有"文件从哪来"：这里完全没有百度网盘的参与，
    所以不需要 Cookie、不会触发限速、也不占用网盘中转目录。
    """
    pipe = Pipeline(cfg, log=log)
    pipe.stop_check = stop_check
    totals = {"total": 0, "done": 0, "failed": 0, "pending": 0,
              "done_bytes": 0, "audio_seconds": 0.0}
    failures: list[str] = []
    aborted = False
    states: list[dict] = []

    for i, raw in enumerate(paths, 1):
        path = Path(raw)
        log(f"\n{'=' * 70}")
        log(f"[{i}/{len(paths)}] {path}")
        log(f"{'=' * 70}")
        try:
            state = pipe.run_local(path, dry_run=dry_run, speakers=speakers)
        except Cancelled as exc:
            log(f"\n已停止：{exc}")
            aborted = True
            break
        except Exception as exc:  # noqa: BLE001
            totals["failed"] += 1
            failures.append(f"{path.name} → {type(exc).__name__}: {exc}")
            log(f"!! 这个文件失败：{type(exc).__name__}: {exc}")
            continue
        states.append(state)
        s = summarize(state)
        for k in ("total", "done", "failed", "pending", "done_bytes"):
            totals[k] += s[k]
        totals["audio_seconds"] += s["audio_seconds"]
        failures.extend(f"{path.name} → {f['file']}: {f['error']}"
                        for f in s["failures"])
    return totals, failures, aborted, states


def needs_retry(states: list[dict]) -> bool:
    """还有没有没跑完的文件（含失败的）。"""
    for state in states:
        for entry in (state.get("files") or {}).values():
            if entry.get("status") != "done":
                return True
    return False


def supervise(run_round, reload_cfg, hours: float, interval_sec: float,
              log=print, sleep=time.sleep, now=time.time) -> dict:
    """反复跑，直到全部完成或超出时间预算。

    为什么需要这个：百度限速下 10GB 要跑两三天，中途几乎一定会遇到
    网络抖动、Cookie 过期、百度风控。让用户盯着屏幕手动重跑并不现实。

    每轮之间**重新加载配置**，所以挂机期间更新了 .env 里的 Cookie，
    下一轮会自动生效——不用重启进程、也不用重新下载已完成的部分。
    """
    deadline = now() + hours * 3600
    round_no = 0
    states: list[dict] = []
    while True:
        round_no += 1
        cfg = reload_cfg()
        log(f"\n{'#' * 68}")
        log(f"# 第 {round_no} 轮　{datetime.now():%Y-%m-%d %H:%M:%S}")
        log(f"{'#' * 68}")
        states = run_round(cfg)

        if not needs_retry(states):
            log("\n全部文件都已完成。")
            return {"rounds": round_no, "states": states, "finished": True}

        remaining = deadline - now()
        if remaining <= 0:
            log(f"\n到达时间预算（{hours:g} 小时），仍有文件没跑完。"
                "重跑同一条命令即可续上，已下载的不会重下。")
            return {"rounds": round_no, "states": states, "finished": False}

        wait = max(min(interval_sec, remaining), 1.0)
        log(f"\n还有未完成的文件，{wait/60:.0f} 分钟后开始下一轮"
            f"（预算还剩 {remaining/3600:.1f} 小时）。")
        log("现在可以去改 .env（比如更新百度 Cookie），下一轮会自动用上。")
        sleep(wait)


def summarize(state: dict) -> dict:
    """统计一条链接的处理情况，用于批量结束后的汇总。"""
    files = state.get("files", {})
    if not files and state.get("saved"):
        # 「只看计划」下还没有任何 files 记录（一个文件都没真正处理），
        # 但 saved 已经解析出来了。这时按 files 统计会报"共 0 个文件"，
        # 让人以为计划是空的——恰恰相反，用户就是想看这个清单。
        return {
            "total": len(state["saved"]),
            "done": 0, "failed": 0, "pending": len(state["saved"]),
            "done_bytes": 0,
            "audio_seconds": sum(
                float(s.get("duration") or 0) for s in state["saved"]),
            "failures": [],
        }
    done = [v for v in files.values() if v.get("status") == "done"]
    failed = [v for v in files.values() if v.get("status") == "failed"]
    pending = [k for k, v in files.items() if v.get("status") not in ("done", "failed")]
    return {
        "total": len(files),
        "done": len(done),
        "failed": len(failed),
        "pending": len(pending),
        "done_bytes": sum(int(v.get("size") or 0) for v in done),
        "audio_seconds": sum(int(v.get("duration_ms") or 0) for v in done) / 1000,
        "failures": [{"file": k, "error": v.get("error", "")}
                     for k, v in files.items() if v.get("status") == "failed"],
    }
