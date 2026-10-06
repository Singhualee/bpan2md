"""配置加载。

优先级：命令行参数 > 环境变量 > 项目根目录的 .env 文件 > 内置默认值。
只依赖标准库，避免因为装不上某个包就整个跑不起来。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 项目根目录：bpan2md/
ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "baidu_pan"
# 默认的配置文件位置。单独提出来是为了让网页界面和测试都能指向别处。
ENV_PATH = ROOT / ".env"


def _parse_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，# 开头是注释，值可以用引号包起来。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


# .env 里读不进来、或读进来了但不能用的键。会被 check() 报给用户，
# 而不是让整个程序起不来——用户手里可能已经有一个坏掉的 .env 了。
_ENV_PROBLEMS: list[str] = []

# Windows 环境变量单个值上限是 32767 字符。留出余量。
MAX_ENV_VALUE = 8000


@dataclass
class Config:
    # ---- 百度网盘 ----
    # 从浏览器复制的整条 Cookie 字符串（含 BDUSS / STOKEN / BAIDUID）
    baidu_cookie: str = ""
    # 转存到自己网盘时的中转目录（避免和已有文件重名）
    transfer_dir: str = "/bpan2md_中转"

    # ---- 转写后端 ----
    # bailian = 阿里云百炼录音文件识别（要一个公网 URL，支持说话人分离）
    # siliconflow = 硅基流动（OpenAI 兼容，直接上传文件，免费模型，无时间戳）
    asr_backend: str = "bailian"

    dashscope_api_key: str = ""
    dashscope_base: str = "https://dashscope.aliyuncs.com/api/v1"
    # 转写完成后用百炼文本模型生成总结和时间大纲。可以关掉以避免额外费用。
    ai_summary: bool = True
    summary_model: str = "qwen3.8-flash"
    # 默认选 paraformer-v2：它同时是「最便宜」（0.288 元/小时）和「支持说话人分离」
    # 的那个，没有理由默认用一个更贵或能力更弱的。详见 bp2md/models.py 的说明。
    bailian_model: str = "paraformer-v2"
    bailian_diarization: bool = True
    bailian_language_hints: str = "zh,en"

    siliconflow_api_key: str = ""
    siliconflow_base: str = "https://api.siliconflow.cn/v1"
    siliconflow_model: str = "FunAudioLLM/SenseVoiceSmall"

    # ---- 公网文件托管（bailian 后端需要）----
    # 任一对象存储都行，只要最终能给出一个公网可访问的 http(s) URL。
    # 内置阿里云 OSS 上传（自己实现签名，不依赖 oss2 —— 它在 Windows 上
    # 会因为 crcmod 没有预编译包而装不上）。
    oss_endpoint: str = ""      # 例如 oss-cn-beijing.aliyuncs.com
    oss_bucket: str = ""
    oss_ak_id: str = ""
    oss_ak_secret: str = ""
    oss_prefix: str = "bpan2md"
    oss_public_read: bool = False  # 私有上传；转写服务使用临时签名 URL
    oss_url_expires: int = 7 * 24 * 3600   # 公共读关闭时，签名 URL 的有效期（秒）
    # 转写完成后删掉 OSS 上的音频（默认保留，方便失败重试时不用重传）
    oss_cleanup: bool = False
    # 如果你已经有别的公网托管方式，可以给一个前缀，脚本会把文件放进去；
    # 留空表示用内置 OSS 上传。
    public_base_url: str = ""
    public_local_dir: str = ""     # 配合 PUBLIC_BASE_URL：把文件拷到哪个本地目录

    # ---- 媒体处理 ----
    audio_bitrate: str = "32k"     # 抽音频用；1 小时≈14MB
    audio_samplerate: str = "16000"
    audio_channels: str = "1"
    # 单段上限：超过就切片（百炼 2GB/12h，听悟 6GB/6h；默认按较严的来）
    max_segment_seconds: int = 6 * 3600
    max_segment_bytes: int = 2 * 1024 ** 3
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"

    # ---- 目录 ----
    workdir: Path = field(default_factory=lambda: ROOT / "work")
    outdir: Path = field(default_factory=lambda: ROOT / "output")

    # ---- 其它 ----
    poll_interval: int = 15
    poll_timeout: int = 4 * 3600

    @property
    def effective_segment_seconds(self) -> int:
        """实际生效的单段时长上限。

        开启说话人分离时会收紧到 2 小时：阿里云官方明确建议
        「启用说话人分离时音频时长不超过 2 小时，否则可能导致识别失败或超时」，
        而配置里的默认上限是 6 小时（为兼容听悟的 6G/6h 限制）。
        不这么做的话，长录音会以失败告终，而错误信息完全指不到真正的原因。

        注意只有在**模型确实支持分离、且我们真的会开**的情况下才收紧。
        """
        limit = self.max_segment_seconds
        if self.asr_backend != "bailian" or not self.bailian_diarization:
            return limit
        from . import models
        spec = models.lookup(self.bailian_model)
        if spec is not None and not spec.diarization:
            return limit      # 模型不支持，实际不会开分离
        cap = models.DIARIZATION_MAX_SECONDS
        return min(limit, cap) if limit > 0 else cap

    @property
    def cookies_json(self) -> Path:
        """Cookie 文件位置。

        从 workdir 派生，不作为独立字段：否则"工作目录在哪"就有了两个答案，
        把 BPAN2MD_WORKDIR 指到别的盘时，凭据文件会留在原来的地方。
        """
        return self.workdir / "cookies.json"

    @property
    def model_spec(self):
        from . import models
        return models.lookup(self.bailian_model)

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        env_path = env_file or ENV_PATH
        dotenv = _parse_dotenv(env_path)

        def val(key: str, default: str = "") -> str:
            """真实环境变量优先，其次 .env 文件，最后默认值。

            这里**刻意不往 os.environ 里写任何东西**。以前的做法是把 .env 的值
            注入环境变量，结果配置在整个进程里"粘住"：改了 .env 不重启不生效，
            而且会隔空影响其它代码（测试里就出现过：一个 .env 让"没配任何东西"
            的用例读到了 Cookie 和 Key）。注入本身就是为了让改动生效而打的补丁，
            直接读文件就不需要这个补丁了。
            """
            if key in os.environ:
                return os.environ[key].strip()
            raw = dotenv.get(key)
            if raw is None or not raw.strip():
                return default
            if len(raw) > MAX_ENV_VALUE:
                # 不让一个坏值把整个程序带崩（Windows 环境变量上限是 32767）
                _ENV_PROBLEMS[:] = [p for p in _ENV_PROBLEMS
                                    if not p.startswith(key + " ")]
                _ENV_PROBLEMS.append(
                    f"{key} 太长（{len(raw)} 字符，上限 {MAX_ENV_VALUE}），已忽略。"
                    f"请在网页「设置」里重新填一个正确的值，"
                    f"或者直接删掉 .env 里 {key} 那一行。"
                )
                return default
            _ENV_PROBLEMS[:] = [p for p in _ENV_PROBLEMS
                                if not p.startswith(key + " ")]
            return raw.strip()

        def val_int(key: str, default: int) -> int:
            try:
                return int(val(key, str(default)))
            except (TypeError, ValueError):
                return default

        def val_bool(key: str, default: bool) -> bool:
            return val(key, "true" if default else "false").lower() in (
                "1", "true", "yes", "on", "是")

        workdir = Path(val("BPAN2MD_WORKDIR", str(ROOT / "work")))
        cfg = cls(
            baidu_cookie=val("BAIDU_COOKIE"),
            transfer_dir=val("BAIDU_TRANSFER_DIR", "/bpan2md_中转"),
            asr_backend=(val("ASR_BACKEND", "bailian")).lower(),
            dashscope_api_key=val("DASHSCOPE_API_KEY"),
            dashscope_base=val("DASHSCOPE_BASE", "https://dashscope.aliyuncs.com/api/v1"),
            ai_summary=val_bool("AI_SUMMARY", True),
            summary_model=val("SUMMARY_MODEL", "qwen3.8-flash"),
            bailian_model=val("BAILIAN_MODEL", "paraformer-v2"),
            bailian_diarization=val_bool("BAILIAN_DIARIZATION", True),
            bailian_language_hints=val("BAILIAN_LANGUAGE_HINTS", "zh,en"),
            siliconflow_api_key=val("SILICONFLOW_API_KEY"),
            siliconflow_base=val("SILICONFLOW_BASE", "https://api.siliconflow.cn/v1"),
            siliconflow_model=val("SILICONFLOW_MODEL", "FunAudioLLM/SenseVoiceSmall"),
            oss_endpoint=val("OSS_ENDPOINT"),
            oss_bucket=val("OSS_BUCKET"),
            oss_ak_id=val("OSS_AK_ID"),
            oss_ak_secret=val("OSS_AK_SECRET"),
            oss_prefix=val("OSS_PREFIX", "bpan2md"),
            oss_public_read=val_bool("OSS_PUBLIC_READ", False),
            oss_url_expires=val_int("OSS_URL_EXPIRES", 7 * 24 * 3600),
            oss_cleanup=val_bool("OSS_CLEANUP", False),
            public_base_url=val("PUBLIC_BASE_URL"),
            public_local_dir=val("PUBLIC_LOCAL_DIR"),
            audio_bitrate=val("AUDIO_BITRATE", "32k"),
            audio_samplerate=val("AUDIO_SAMPLERATE", "16000"),
            audio_channels=val("AUDIO_CHANNELS", "1"),
            max_segment_seconds=val_int("MAX_SEGMENT_SECONDS", 6 * 3600),
            max_segment_bytes=val_int("MAX_SEGMENT_BYTES", 2 * 1024 ** 3),
            ffmpeg=val("FFMPEG", "ffmpeg"),
            ffprobe=val("FFPROBE", "ffprobe"),
            poll_interval=val_int("POLL_INTERVAL", 15),
            poll_timeout=val_int("POLL_TIMEOUT", 4 * 3600),
            workdir=workdir,
            outdir=Path(val("BPAN2MD_OUTDIR", str(ROOT / "output"))),
        )
        for sub in ("raw", "audio", "segments", "state", "result", "uploads"):
            (cfg.workdir / sub).mkdir(parents=True, exist_ok=True)
        cfg.outdir.mkdir(parents=True, exist_ok=True)
        return cfg

    # ---------- 自检 ----------
    def check(self, require_baidu: bool = True) -> list[str]:
        """返回配置问题列表（空列表表示可以跑）。

        require_baidu=False 用于「只验证云端转写链路」的场景（doctor），
        这时不需要百度凭据。
        """
        problems: list[str] = []
        problems.extend(_ENV_PROBLEMS)
        if require_baidu and not self.baidu_cookie and not self.cookies_json.exists():
            problems.append(
                "缺少百度网盘凭据：请在 .env 里填 BAIDU_COOKIE，"
                f"或先准备好 {self.cookies_json}"
            )
        if self.asr_backend == "bailian":
            if not self.dashscope_api_key:
                problems.append("ASR_BACKEND=bailian 需要 DASHSCOPE_API_KEY")
            from . import models as _models
            spec = _models.lookup(self.bailian_model)
            if spec is None:
                problems.append(
                    f"BAILIAN_MODEL={self.bailian_model} 不在已知登记表里，"
                    f"价格和能力无法核对（不影响运行，但请自行确认它支持录音文件转写）"
                )
            else:
                if not spec.needs_public_url:
                    problems.append(
                        f"BAILIAN_MODEL={self.bailian_model} 不是录音文件转写（异步）模型，"
                        f"本工具用的是 /services/audio/asr/transcription 接口"
                    )
                if self.bailian_diarization and not spec.diarization:
                    problems.append(
                        f"{self.bailian_model} 不支持说话人分离，但 BAILIAN_DIARIZATION=true。"
                        f"要么关掉分离，要么改用 paraformer-v2"
                        f"（0.288 元/小时，同样支持分离，还更便宜）"
                    )
            if not self.public_base_url:
                if not (self.oss_endpoint and self.oss_bucket
                        and self.oss_ak_id and self.oss_ak_secret):
                    problems.append(
                        "ASR_BACKEND=bailian 需要一个公网可访问的文件 URL："
                        "请配置 OSS_ENDPOINT/OSS_BUCKET/OSS_AK_ID/OSS_AK_SECRET，"
                        "或设置 PUBLIC_BASE_URL 指向你自己的托管"
                    )
        elif self.asr_backend == "siliconflow":
            if not self.siliconflow_api_key:
                problems.append("ASR_BACKEND=siliconflow 需要 SILICONFLOW_API_KEY")
        else:
            problems.append(f"未知的 ASR_BACKEND: {self.asr_backend}")
        return problems

    def redacted(self) -> dict[str, str]:
        def mask(v: str) -> str:
            if not v:
                return "(未设置)"
            return v[:6] + "..." + v[-4:] if len(v) > 12 else "(已设置)"

        return {
            "百度 Cookie": mask(self.baidu_cookie),
            "转写后端": self.asr_backend,
            "百炼 API Key": mask(self.dashscope_api_key),
            "AI 总结": self.summary_model if self.ai_summary else "关闭",
            "百炼模型": self.bailian_model,
            "模型能力": _model_summary(self),
            "说话人分离": "开" if self.bailian_diarization else "关",
            "单段上限": f"{self.effective_segment_seconds/3600:.1f} 小时",
            "硅基流动 API Key": mask(self.siliconflow_api_key),
            "硅基流动模型": self.siliconflow_model,
            "OSS 桶": self.oss_bucket or "(未设置)",
            "公共 URL 前缀": self.public_base_url or "(未设置)",
            "工作目录": str(self.workdir),
            "输出目录": str(self.outdir),
        }


def _model_summary(cfg: "Config") -> str:
    from . import models
    if cfg.asr_backend != "bailian":
        return "（仅百炼后端适用）"
    spec = models.lookup(cfg.bailian_model)
    if spec is None:
        return "不在登记表里，请以官方文档为准"
    parts = []
    if spec.price_per_second is not None:
        parts.append(f"{spec.price_per_second*3600:.3f} 元/小时")
    else:
        parts.append("按 token 计费")
    parts.append("支持分离" if spec.diarization else "不支持分离")
    if spec.max_seconds:
        parts.append(f"单段 ≤{spec.max_seconds/3600:.0f} 小时"
                     if spec.max_seconds >= 3600
                     else f"单段 ≤{spec.max_seconds/60:.0f} 分钟")
    return "，".join(parts)


# --------------------------------------------------------------------------
# 写 .env（给网页界面用）
# --------------------------------------------------------------------------

# 允许从界面修改的键。白名单是刻意的：不让界面往 .env 里写任意内容。
EDITABLE_KEYS = (
    "BAIDU_COOKIE",
    "ASR_BACKEND",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_BASE",
    "AI_SUMMARY",
    "SUMMARY_MODEL",
    "BAILIAN_MODEL",
    "BAILIAN_DIARIZATION",
    "OSS_ENDPOINT",
    "OSS_BUCKET",
    "OSS_AK_ID",
    "OSS_AK_SECRET",
    "OSS_PUBLIC_READ",
    "SILICONFLOW_API_KEY",
    "BAIDU_TRANSFER_DIR",
    "MAX_SEGMENT_SECONDS",
)


def update_env_file(path: Path, updates: dict[str, str]) -> list[str]:
    """把若干键写进 .env，**保留原有的注释和其它键**。

    返回实际写入的键。只接受 EDITABLE_KEYS 里的键，其余忽略。
    值里的换行会被去掉（Cookie 经常是从浏览器复制的一整行）。

    超长的值直接拒绝并抛 ValueError。这条防线是必须的：曾经有个 300 万字符的
    值被写进 .env，而 Windows 的环境变量上限是 32767 —— 之后每次读配置都抛
    ValueError，整个程序起不来，而用户完全不知道该怎么修。
    """
    path = Path(path)
    clean: dict[str, str] = {}
    for k, v in updates.items():
        if k not in EDITABLE_KEYS or v is None:
            continue
        text = str(v).replace("\r", "").replace("\n", "").strip()
        if len(text) > MAX_ENV_VALUE:
            raise ValueError(
                f"{k} 太长了（{len(text)} 字符，上限 {MAX_ENV_VALUE}）。"
                f"百度 Cookie 一般只有一两千字符——是不是把整段网页内容复制进来了？"
            )
        clean[k] = text

    if not clean:
        return []

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    written: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in clean:
                out.append(f"{key}={clean[key]}")
                written.add(key)
                continue
        out.append(line)

    leftover = {k: v for k, v in clean.items() if k not in written}
    if leftover:
        if out and out[-1].strip():
            out.append("")
        out.append("# ---- 由网页界面写入 ----")
        for k, v in leftover.items():
            out.append(f"{k}={v}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)     # 里面是账号凭据
    except OSError:
        pass
    return sorted(clean)
