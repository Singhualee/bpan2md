"""用百炼文本模型为转写稿生成可核对的总结和时间大纲。"""

from __future__ import annotations

import json

from . import http
from .asr import Transcript
from .config import Config
from .md import concise_text, format_ts

MAX_CHARS = 12_000


def _source(transcript: Transcript) -> str:
    return "\n".join(
        f"[{format_ts(s.start_ms, force_hours=True)}] {concise_text(s.text)}"
        for s in transcript.segments if concise_text(s.text)
    )


def _call(cfg: Config, prompt: str) -> str:
    # qwen3.8-flash 在百炼的 OpenAI 兼容接口上可用；旧 generation 路径会
    # 返回 InvalidParameter/url error，不能把这个错误静默降级成抽句大纲。
    url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    response = http.post(url, headers={
        "Authorization": f"Bearer {cfg.dashscope_api_key}",
        "Content-Type": "application/json",
    }, json={"model": cfg.summary_model, "messages": [
        {"role": "system", "content": "你是严谨的中文编辑。只依据给出的转写稿总结，不补充外部事实。"},
        {"role": "user", "content": prompt},
    ], "temperature": 0.2, "max_tokens": 1600},
    what="百炼 AI 总结", retry_connection=False, timeout=90)
    if not response.ok:
        raise RuntimeError(f"百炼 AI 总结失败：HTTP {response.status_code} {response.text[:300]}")
    data = response.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    if not content.strip():
        raise RuntimeError("百炼 AI 总结返回为空")
    return content.strip()


def generate(cfg: Config, transcript: Transcript, title: str) -> dict:
    """返回可直接写进 Markdown 的 summary / outline；长稿先分段再汇总。"""
    source = _source(transcript)
    if not source:
        return {"summary": "", "outline": "", "model": cfg.summary_model}
    chunks = [source[i:i + MAX_CHARS] for i in range(0, len(source), MAX_CHARS)]
    notes = []
    for i, chunk in enumerate(chunks, 1):
        notes.append(_call(cfg, f"以下是《{title}》转写稿的第 {i}/{len(chunks)} 部分。"
                                  "提取事实、观点、论证和建议，删除口头禅。"
                                  "用 5—10 条简洁要点输出，每条保留原有时间戳。\n\n" + chunk))
    material = "\n\n".join(notes)
    answer = _call(cfg, f"根据以下分段要点，为《{title}》写 Markdown。"
                         "先输出 `## 内容总结`，包含 3—6 条准确、简洁的核心结论；"
                         "再输出 `## 内容大纲`，按时间顺序列出 5—12 条主题节点，"
                         "每条必须保留形如 [00:12:34] 的时间戳。不要写开场白、免责声明或代码块。\n\n"
                         + material)
    if "## 内容大纲" not in answer:
        answer += "\n\n## 内容大纲\n\n" + material
    summary, outline = answer.split("## 内容大纲", 1)
    return {"summary": summary.replace("## 内容总结", "").strip(),
            "outline": outline.strip(), "model": cfg.summary_model}
