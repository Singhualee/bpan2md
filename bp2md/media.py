"""媒体处理：探测时长、抽音频、超限切片。

关键点：**先抽音频再上传**。1 小时视频抽成 16kHz 单声道 32kbps 的 mp3
大约只有 14MB，是原视频的几十分之一；而转写 API 是按音频时长计费的，
抽音频不会多花钱，却能把上传时间压到几乎可以忽略。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .config import Config

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".m4v",
             ".rmvb", ".ts", ".mpg", ".mpeg", ".3gp", ".dat", ".vob"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma", ".amr",
             ".opus", ".aiff", ".aif", ".ape"}


class MediaError(RuntimeError):
    pass


def _run(cmd: list[str], log=None) -> subprocess.CompletedProcess:
    if log:
        log("$ " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise MediaError("命令失败: " + " ".join(cmd[:3]) + "\n" + "\n".join(tail))
    return proc


def is_media(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXT | AUDIO_EXT


def find_media(root: Path, deep: bool = True, cap: int = 2000) -> tuple[list, bool]:
    """列出一个目录里的音视频文件。返回 (文件列表, 是否被截断)。

    命令行和网页界面共用：网页里"扫描文件夹"用的就是它，所以两边认的
    扩展名一定一致（只认 media.is_media 认可的那些）。
    """
    root = Path(root)
    found: list[Path] = []
    if not deep:
        for f in sorted(root.iterdir(), key=lambda x: str(x).lower()):
            if f.is_file() and is_media(f) and not f.name.endswith(".part"):
                found.append(f)
                if len(found) >= cap:
                    return found, True
        return found, False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            f = Path(dirpath) / fn
            if is_media(f) and not fn.endswith(".part"):
                found.append(f)
                if len(found) >= cap:
                    return found, True
    return found, False


def probe_duration(cfg: Config, path: Path) -> float:
    """返回时长（秒）。探测不到返回 0。"""
    proc = _run([
        cfg.ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    ])
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return 0.0


def probe_stream_info(cfg: Config, path: Path) -> dict:
    proc = _run([
        cfg.ffprobe, "-v", "error", "-show_entries",
        "stream=codec_type,channels,sample_rate", "-of", "json", str(path),
    ])
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    return {"has_audio": bool(audio), "audio_streams": len(audio)}


def extract_audio(cfg: Config, src: Path, dst: Path, log=print) -> Path:
    """抽成单声道低码率 mp3。有音轨的视频、纯音频文件都走这里。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn",
        "-ac", cfg.audio_channels,
        "-ar", cfg.audio_samplerate,
        "-b:a", cfg.audio_bitrate,
        str(dst),
    ], log=log)
    if not dst.exists() or dst.stat().st_size == 0:
        raise MediaError(f"抽音频没有产出有效文件: {src}")
    return dst


def split_by_time(cfg: Config, src: Path, out_dir: Path,
                  seconds: int, log=print) -> list[Path]:
    """按时间切片（-c copy，不重新编码，很快）。用于超过单段上限的长文件。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = out_dir / f"{src.stem}_part%03d{src.suffix}"
    _run([
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-c", "copy", "-map", "0",
        "-f", "segment",
        "-segment_time", str(int(seconds)),
        "-reset_timestamps", "1",
        str(pattern),
    ], log=log)
    parts = sorted(out_dir.glob(f"{src.stem}_part*{src.suffix}"))
    if not parts:
        raise MediaError(f"切片没有产出文件: {src}")
    return parts


def prepare(cfg: Config, src: Path, log=print,
            stem: str | None = None) -> list[tuple[Path, float]]:
    """把一个源文件变成「可上传转写的音频片段」列表。

    返回 [(音频路径, 该片段相对整段的起始偏移秒), ...]。

    `stem` 用来指定中间产物的名字。默认取文件名——但**本地文件**那条路会
    传一个带内容指纹的 stem：同一个文件名被换成另一份内容时，中间产物必须
    落到另一个文件上，否则会命中上一次的音频缓存，算出一份对不上的稿子。

    流程刻意是「先抽音频，再按需切音频」而不是「先切视频」：
    切视频要用 -c copy，而 -c copy 只能在关键帧处下刀，关键帧稀疏的视频
    会切出远超上限的段（那是 API 的硬限制，超了就被拒）。mp3 是帧格式，
    同样用 -c copy 切也能精确到几十毫秒，而且文件小、切得快。
    """
    duration = probe_duration(cfg, src)
    size = src.stat().st_size
    stem = stem or src.stem
    audio_dir = cfg.workdir / "audio"
    seg_dir = cfg.workdir / "segments" / stem

    # 先确认真的有音轨：有些分享出来的是无声录屏或纯图片视频，
    # 直接丢给 ffmpeg 抽音频只会得到一个难懂的报错
    info = probe_stream_info(cfg, src)
    if not info.get("has_audio"):
        raise MediaError(
            f"这个文件没有音轨，无法转写：{src.name}"
            f"（时长 {duration/60:.1f} 分钟，可能是个无声录屏或纯图片视频）"
        )
    if info.get("audio_streams", 1) > 1:
        log(f"[media] 检测到 {info['audio_streams']} 条音轨，只会转写第一条")

    audio = audio_dir / f"{stem}.mp3"
    if audio.exists() and audio.stat().st_size > 0:
        log(f"[media] 复用已有音频: {audio.name}")
    else:
        log(f"[media] 抽取音频（源 {duration/60:.1f} 分钟, {size/1024/1024:.1f} MB）")
        extract_audio(cfg, src, audio, log=log)
    audio_duration = probe_duration(cfg, audio) or duration
    audio_size = audio.stat().st_size
    log(f"[media] 音频 {audio_size/1024/1024:.1f} MB / {audio_duration/60:.1f} 分钟")

    limit = cfg.effective_segment_seconds
    if limit and cfg.asr_backend == "bailian" and cfg.bailian_diarization \
            and limit < cfg.max_segment_seconds:
        log(f"[media] 开启了说话人分离，单段上限自动收紧到 "
            f"{limit/3600:.1f} 小时（官方建议 ≤2 小时）")

    over_time = limit > 0 and audio_duration > limit
    over_size = cfg.max_segment_bytes > 0 and audio_size > cfg.max_segment_bytes
    if not over_time and not over_size:
        return [(audio, 0.0)]

    if over_time:
        seg_len = limit
        log(f"[media] 时长 {audio_duration/3600:.2f}h 超过上限 "
            f"{seg_len/3600:.2f}h，切成 {seg_len/60:.0f} 分钟一段")
    else:
        ratio = cfg.max_segment_bytes / audio_size
        seg_len = max(int(audio_duration * ratio * 0.9), 60)
        log(f"[media] 体积 {audio_size/1024**3:.2f}GB 超过上限，"
            f"切成 {seg_len/60:.1f} 分钟一段")

    parts = sorted(seg_dir.glob(f"{stem}_part*.mp3"))
    if not parts:
        parts = split_by_time(cfg, audio, seg_dir, seg_len, log=log)
    if len(parts) < 2:
        log("[media] 警告：切片后仍只有 1 段，可能这个文件本身很短")

    out: list[tuple[Path, float]] = []
    offset = 0.0
    for part in parts:
        out.append((part, offset))
        offset += probe_duration(cfg, part)
    log(f"[media] 切成 {len(out)} 段，合计约 {offset/60:.1f} 分钟")
    return out
