"""转写后端。两个实现，输出统一的 Transcript 结构。

- BailianAsr     阿里云百炼「录音文件识别」（异步）。需要一个公网 URL。
                 支持说话人分离；按音频秒数或 token 计费。
- SiliconFlowAsr 硅基流动（OpenAI 兼容），multipart 直接传文件，不用托管。
                 SenseVoiceSmall 等模型目前免费；代价是接口不返回逐句时间戳。

统一结构让后面的 Markdown / 分块输出完全不用关心用的哪家。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import http, models
from .config import Config


class AsrError(RuntimeError):
    pass


@dataclass
class Segment:
    start_ms: int
    end_ms: int
    text: str
    speaker: str = ""


@dataclass
class Transcript:
    segments: list[Segment] = field(default_factory=list)
    duration_ms: int = 0
    model: str = ""
    backend: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)

    @property
    def speaker_count(self) -> int:
        """不同说话人的个数（0 表示没有分离信息）。

        判定单人/多人**不需要预先探测**：分离开启时结果里本来就带 speaker_id，
        直接数一下就是零成本的。
        """
        seen = {s.speaker for s in self.segments if s.speaker}
        return len(seen)


# ==========================================================================
# 阿里云百炼：录音文件识别（异步）
# ==========================================================================

class BailianAsr:
    """POST /services/audio/asr/transcription + X-DashScope-Async: enable

    提交任务 → 轮询 /tasks/{id} → 下载 transcription_url 指向的 JSON。
    那个 JSON 才是真正的转写结果：transcripts[].sentences[]，
    每句带 begin_time / end_time（毫秒）和（开启分离时的）speaker_id。
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.dashscope_api_key:
            raise AsrError("缺少 DASHSCOPE_API_KEY")
        self.base = cfg.dashscope_base.rstrip("/")
        self.spec = models.lookup(cfg.bailian_model)
        if self.spec and not self.spec.needs_public_url:
            raise AsrError(
                f"{cfg.bailian_model} 不是录音文件转写（异步）模型，本工具用的是 "
                f"/services/audio/asr/transcription 接口。请换成 filetrans / fun-asr / "
                f"paraformer 系列。"
            )

    def _headers(self, async_call: bool = False) -> dict:
        h = {
            "Authorization": f"Bearer {self.cfg.dashscope_api_key}",
            "Content-Type": "application/json",
        }
        if async_call:
            h["X-DashScope-Async"] = "enable"
        return h

    def transcribe(self, file_url: str, log=print) -> Transcript:
        task_id = self._submit(file_url, log=log)
        return self._wait_and_fetch(task_id, log=log)

    def _submit(self, file_url: str, log=print) -> str:
        params: dict = {}
        spec = self.spec

        if self.cfg.bailian_language_hints:
            # 官方示例里 language_hints 只出现在 qwen-audio 系列上；
            # 对其它模型传了可能不被接受，所以按登记表决定要不要发
            if spec is None or spec.language_hints:
                params["language_hints"] = [
                    x.strip() for x in self.cfg.bailian_language_hints.split(",")
                    if x.strip()
                ]
            else:
                log(f"[asr] {self.cfg.bailian_model} 的官方示例未使用 language_hints，已跳过")

        if self.cfg.bailian_diarization:
            if spec is None:
                # 新模型没登记：按 filetrans 系列的通用参数名发，并提醒一下
                params["diarization_enabled"] = True
                log(f"[asr] 警告：{self.cfg.bailian_model} 不在已知登记表里，"
                    f"按通用参数 diarization_enabled 提交；若结果里没有 speaker_id，"
                    f"说明该模型其实不支持说话人分离")
            elif not spec.diarization:
                raise AsrError(
                    f"{self.cfg.bailian_model} 不支持说话人分离（官方能力表明确列出）。\n"
                    f"要么把 BAILIAN_DIARIZATION 改成 false，"
                    f"要么换成支持分离的模型——最便宜的是 paraformer-v2"
                    f"（0.288 元/小时，同样支持分离）。"
                )
            else:
                params[spec.diarization_param or "diarization_enabled"] = True

        payload = {
            "model": self.cfg.bailian_model,
            "input": {"file_urls": [file_url]},
            "parameters": params,
        }
        url = f"{self.base}/services/audio/asr/transcription"
        log(f"[asr] 提交任务 {models.describe(self.cfg.bailian_model)}")
        # 提交是有副作用的请求：连接错误不重试（可能任务已创建，重试会重复计费），
        # 只对明确的 429/5xx 重试。
        resp = http.post(url, headers=self._headers(async_call=True), json=payload,
                         log=log, what="提交转写任务", retry_connection=False)
        if resp.status_code != 200:
            raise AsrError(f"提交任务失败 HTTP {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise AsrError(f"没拿到 task_id，返回: {json.dumps(data, ensure_ascii=False)[:400]}")
        log(f"[asr] task_id={task_id}")
        return task_id

    def _wait_and_fetch(self, task_id: str, log=print) -> Transcript:
        url = f"{self.base}/tasks/{task_id}"
        deadline = time.time() + self.cfg.poll_timeout
        last = ""
        consecutive_errors = 0
        while time.time() < deadline:
            try:
                resp = http.get(url, headers=self._headers(), timeout=60,
                                log=log, what="查询任务状态")
                if resp.status_code != 200:
                    raise AsrError(f"查询任务失败 HTTP {resp.status_code}: {resp.text[:300]}")
                data = resp.json()
                consecutive_errors = 0
            except AsrError:
                raise
            except Exception as exc:
                # 轮询期间网络抖动不该让几小时的任务前功尽弃
                consecutive_errors += 1
                if consecutive_errors >= 10:
                    raise AsrError(f"连续 {consecutive_errors} 次查询失败: {exc}") from exc
                log(f"[asr] 查询出错（{consecutive_errors}/10），稍后继续: {str(exc)[:100]}")
                time.sleep(self.cfg.poll_interval)
                continue

            out = data.get("output") or {}
            status = str(out.get("task_status") or "").upper()
            if status != last:
                log(f"[asr] 状态: {status or '(空)'}")
                last = status
            if status == "SUCCEEDED":
                return self._collect(out, log=log)
            if status in ("FAILED", "UNKNOWN"):
                raise AsrError(
                    "转写任务失败: " + json.dumps(out, ensure_ascii=False)[:400]
                )
            time.sleep(self.cfg.poll_interval)
        raise AsrError(f"等待超时（{self.cfg.poll_timeout}s），任务 {task_id} 可能仍在跑，"
                       "可凭 task_id 重新查询")

    def _collect(self, out: dict, log=print) -> Transcript:
        results = out.get("results") or []
        if not results:
            raise AsrError("任务成功但没有 results: " + json.dumps(out, ensure_ascii=False)[:300])
        first = results[0]
        sub = str(first.get("subtask_status") or "").upper()
        if sub and sub != "SUCCEEDED":
            raise AsrError(f"子任务失败({sub}): " + json.dumps(first, ensure_ascii=False)[:300])
        turl = first.get("transcription_url")
        if not turl:
            raise AsrError("结果里没有 transcription_url: "
                           + json.dumps(first, ensure_ascii=False)[:300])
        log("[asr] 下载识别结果 JSON")
        resp = http.get(turl, timeout=300, log=log, what="下载识别结果")
        resp.raise_for_status()
        return self.parse_result(resp.json())

    @staticmethod
    def parse_result(raw: dict) -> Transcript:
        segments: list[Segment] = []
        duration = int((raw.get("properties") or {}).get(
            "original_duration_in_milliseconds") or 0)
        for tr in raw.get("transcripts") or []:
            for st in tr.get("sentences") or []:
                text = (st.get("text") or "").strip()
                if not text:
                    continue
                speaker = st.get("speaker_id")
                segments.append(Segment(
                    start_ms=int(st.get("begin_time") or 0),
                    end_ms=int(st.get("end_time") or 0),
                    text=text,
                    speaker="" if speaker is None else str(speaker),
                ))
        if not duration and segments:
            duration = segments[-1].end_ms
        return Transcript(segments=segments, duration_ms=duration, raw=raw)


# ==========================================================================
# 硅基流动：OpenAI 兼容的 /audio/transcriptions（multipart 直传）
# ==========================================================================

class SiliconFlowAsr:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        if not cfg.siliconflow_api_key:
            raise AsrError("缺少 SILICONFLOW_API_KEY")
        self.base = cfg.siliconflow_base.rstrip("/")

    def transcribe(self, audio: Path, log=print) -> Transcript:
        url = f"{self.base}/audio/transcriptions"
        log(f"[asr] 上传转写 model={self.cfg.siliconflow_model} 文件={audio.name}")

        def once():
            # 每次重试都要重新打开文件（上次失败时句柄已经读到末尾了）
            with open(audio, "rb") as fh:
                return http.post(
                    url,
                    headers={"Authorization": f"Bearer {self.cfg.siliconflow_api_key}"},
                    files={"file": (audio.name, fh, "audio/mpeg")},
                    data={"model": self.cfg.siliconflow_model},
                    timeout=1800, log=log, what="上传转写",
                )

        resp = once()
        if resp.status_code != 200:
            raise AsrError(f"转写失败 HTTP {resp.status_code}: {resp.text[:400]}")
        try:
            payload = resp.json()
        except json.JSONDecodeError:
            payload = {"text": resp.text}

        segments: list[Segment] = []
        if isinstance(payload.get("segments"), list):
            for i, seg in enumerate(payload["segments"]):
                text = (seg.get("text") or "").strip()
                if not text:
                    continue
                segments.append(Segment(
                    start_ms=int(float(seg.get("start") or 0) * 1000),
                    end_ms=int(float(seg.get("end") or 0) * 1000),
                    text=text,
                    speaker=str(seg.get("speaker") or ""),
                ))
        if not segments:
            # SenseVoice 这类模型只回一段纯文本，没有时间戳；
            # 按中英文句末标点切成若干段，便于知识库切块。
            text = (payload.get("text") or "").strip()
            segments = _split_plain_text(text)
        return Transcript(segments=segments, model=self.cfg.siliconflow_model,
                          backend="siliconflow", raw=payload)


def _split_plain_text(text: str, max_chars: int = 400) -> list[Segment]:
    """没有时间戳时，按标点把长文本切成段落（时间戳留空）。"""
    if not text:
        return []
    import re
    pieces = re.split(r"(?<=[。！？!?；;])\s*", text)
    out: list[Segment] = []
    buf = ""
    for piece in pieces:
        if not piece:
            continue
        if len(buf) + len(piece) > max_chars and buf:
            out.append(Segment(0, 0, buf.strip()))
            buf = piece
        else:
            buf += piece
    if buf.strip():
        out.append(Segment(0, 0, buf.strip()))
    return out


def make_asr(cfg: Config):
    if cfg.asr_backend == "bailian":
        return BailianAsr(cfg)
    if cfg.asr_backend == "siliconflow":
        return SiliconFlowAsr(cfg)
    raise AsrError(f"未知的 ASR_BACKEND: {cfg.asr_backend}")
