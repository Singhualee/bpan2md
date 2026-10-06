"""把转写结果写成「给知识库用」的 Markdown 和分块 JSONL。

面向检索的设计取舍：
- YAML front matter 带全部元数据（来源、时长、模型、说话人…），
  Obsidian / Logseq / 思源 都能直接识别；
- 正文按时间窗切成 H2 小节，每句独立成段并带 [hh:mm:ss] 前缀，
  这样检索命中后能给出「哪个文件、第几分钟」的引用；
- 额外产出一份 chunks.jsonl（每块含起止时间、说话人、正文），
  向量库入库时不用再自己切分。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .asr import Segment, Transcript

# 一个 H2 小节覆盖的时间跨度
SECTION_SECONDS = 600
# 分块目标长度（字符）
CHUNK_TARGET_CHARS = 500
CHUNK_MAX_CHARS = 900
# 相邻句子间隔超过这个秒数就强制断块（通常是话题切换）
CHUNK_GAP_SECONDS = 8

# 只处理明显不承载信息的口头填充词。原始转写始终保存在 result.json，
# 这里不做激进的"润色"，避免为了简洁删掉论点或改变原意。
_FILLER_ONLY = re.compile(r"^[啊嗯呃唉哎哦]+[，。！？、\s]*$")
_FILLER_PREFIX = re.compile(r"^(?:啊|嗯|呃|唉|哎|哦)[，、\s]*")
_FILLER_PHRASES = re.compile(
    r"(?:很多的时候来讲|很多时候来讲|可以说|就是说|然后呢|这个呢|那么呢)[，、\s]*")


def concise_text(text: str) -> str:
    """清掉展示层中明显的口水话，保守地保留原句意思。"""
    text = _FILLER_PREFIX.sub("", str(text or "").strip())
    text = _FILLER_PHRASES.sub("", text)
    text = re.sub(r"([，。！？])\1+", r"\1", text)
    return text.strip()


def _meaningful_segments(segs: list[Segment]) -> list[Segment]:
    """过滤掉仅含「嗯 / 啊 / 好」一类确认词的独立片段。"""
    out = []
    for seg in segs:
        raw = str(seg.text or "").strip()
        text = concise_text(raw)
        if not text or _FILLER_ONLY.fullmatch(raw) or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", text):
            continue
        out.append(Segment(seg.start_ms, seg.end_ms, text, seg.speaker))
    return out


def _outline(segs: list[Segment]) -> list[Segment]:
    """按时间均匀抽取信息密度较高的句子，作为零额外费用的大纲。

    这是提取式大纲，不会假装成 AI 改写摘要；每一条都能回到原文时间戳核对。
    """
    if not segs:
        return []
    slots = min(6, max(1, (segs[-1].start_ms - segs[0].start_ms) // 120_000 + 1))
    span = max(segs[-1].start_ms - segs[0].start_ms + 1, 1)
    chosen: list[Segment] = []
    for slot in range(slots):
        left = segs[0].start_ms + span * slot // slots
        right = segs[0].start_ms + span * (slot + 1) // slots
        candidates = [s for s in segs if left <= s.start_ms < right]
        if not candidates:
            continue
        # 太短通常是承接语；过长常是 ASR 未断句。优先取中等长度的完整论述。
        chosen.append(max(candidates, key=lambda s: min(len(s.text), 80)
                      - (35 if len(s.text) < 12 else 0)))
    return chosen


def format_ts(ms: int, force_hours: bool = False) -> str:
    """毫秒 → hh:mm:ss（不足一小时时默认 mm:ss）。"""
    total = max(int(ms), 0) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h or force_hours:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def slugify(name: str, fallback: str = "transcript") -> str:
    """生成安全文件名，保留中文。

    注意顺序：先把路径分隔符和非法字符统一替换掉，再取扩展名。
    反过来的话，名字里带 / 或 \\ 会被 Path 当成目录，取到错误的 stem。
    """
    raw = unicodedata.normalize("NFKC", str(name))
    raw = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", raw)
    stem = Path(raw).stem if raw else ""
    stem = re.sub(r"\s+", "_", stem).strip("._")
    return (stem or fallback)[:80]


def _yaml_str(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


@dataclass
class DocMeta:
    title: str
    source_file: str
    source_url: str = ""
    duration_ms: int = 0
    asr_backend: str = ""
    asr_model: str = ""
    diarization: bool = False
    language: str = "zh"
    transcribed_at: str = ""
    extra: dict | None = None
    # 是否在正文里渲染「发言人 N：」前缀。
    # 单人内容渲染这个只会让正文变脏，所以由说话人数自动决定。
    speaker_labels: bool = True
    # 由几段拼起来的（>1 时各段的发言人编号互相独立）
    parts: int = 1
    # 百炼文本模型生成的内容总结与大纲；随 result.json 缓存，render 不会重复调用。
    ai_summary: dict | None = None


def _speakers(segments: list[Segment]) -> list[str]:
    seen: list[str] = []
    for seg in segments:
        if seg.speaker and seg.speaker not in seen:
            seen.append(seg.speaker)
    return seen


def speaker_count(segments: list[Segment]) -> int:
    """不同说话人的个数。0 表示没有分离信息。"""
    return len(_speakers(segments))


def _labels_on(meta: DocMeta, spk: list[str]) -> bool:
    """正文是否渲染「发言人 N：」。

    单人内容渲染它只会让正文变脏：整篇都是「发言人 1：」，对知识库毫无增益。
    所以只要实际只出现一个说话人，就自动不渲染——说话人数仍然记在
    front matter 的 speaker_count 里，检索时照旧可用。
    """
    if not meta.speaker_labels:
        return False
    return len(spk) >= 2


def _front_matter(meta: DocMeta, segments: list[Segment]) -> str:
    spk = _speakers(segments)
    lines = ["---"]
    lines.append(f"title: {_yaml_str(meta.title)}")
    lines.append(f"source_file: {_yaml_str(meta.source_file)}")
    if meta.source_url:
        lines.append(f"source_url: {_yaml_str(meta.source_url)}")
    lines.append(f"duration: {_yaml_str(format_ts(meta.duration_ms, force_hours=True))}")
    lines.append(f"duration_seconds: {meta.duration_ms // 1000}")
    lines.append(f"transcribed_at: {_yaml_str(meta.transcribed_at)}")
    lines.append(f"asr_backend: {_yaml_str(meta.asr_backend)}")
    lines.append(f"asr_model: {_yaml_str(meta.asr_model)}")
    lines.append(f"speaker_diarization: {'true' if meta.diarization else 'false'}")
    lines.append(f"speaker_count: {len(spk)}")
    if spk:
        lines.append("speakers: [" + ", ".join(_yaml_str(s) for s in spk) + "]")
    lines.append(f"speaker_labels: {'true' if _labels_on(meta, spk) else 'false'}")
    if meta.parts > 1:
        lines.append(f"parts: {meta.parts}")
    lines.append(f"language: {_yaml_str(meta.language)}")
    lines.append("tags: [转写稿, 音视频]")
    if meta.extra:
        for k, v in meta.extra.items():
            lines.append(f"{k}: {_yaml_str(v)}")
    lines.append("---")
    return "\n".join(lines)


def build_markdown(transcript: Transcript, meta: DocMeta) -> str:
    raw_segs = transcript.segments
    segs = _meaningful_segments(raw_segs)
    parts: list[str] = [_front_matter(meta, raw_segs), ""]
    parts.append(f"# {meta.title}")
    parts.append("")
    summary = (f"> 来源文件：{meta.source_file}　｜　时长 "
               f"{format_ts(transcript.duration_ms, force_hours=True)}　｜　"
               f"转写：{meta.asr_backend}/{meta.asr_model}")
    parts.append(summary)
    if meta.source_url:
        parts.append(f">\n> 分享链接：{meta.source_url}")
    parts.append("")

    if not segs:
        parts.append("_(没有识别到任何文字)_")
        return "\n".join(parts) + "\n"

    ai = meta.ai_summary or {}
    if ai.get("summary") and ai.get("outline"):
        parts.extend(["## 内容总结", "", str(ai["summary"]), "",
                      "## 内容大纲", "", str(ai["outline"]), ""])
    else:
        outline = _outline(segs)
        parts.extend(["## 内容总结", "",
                      "以下为从原文提取的核心表述；可通过时间戳回到正文核对。", ""])
        for seg in outline[:3]:
            parts.append(f"- {seg.text}")
        parts.extend(["", "## 内容大纲", ""])
        for seg in outline:
            parts.append(f"- [{format_ts(seg.start_ms, force_hours=True)}] {seg.text}")
        parts.append("")

    show_labels = _labels_on(meta, _speakers(segs))
    if meta.parts > 1 and meta.diarization:
        parts.append("> ⚠️ 本文件由多段分别转写后拼接，"
                     "**各段内部的发言人编号互相独立**——"
                     "「发言人 1」在第一段和第三段未必是同一个人。")
        parts.append("")

    # 按时间窗分 H2
    current_section = None
    for seg in segs:
        section = int(seg.start_ms // 1000 // SECTION_SECONDS) * SECTION_SECONDS
        if section != current_section:
            current_section = section
            end = section + SECTION_SECONDS
            parts.append("")
            parts.append(f"## {format_ts(section * 1000, force_hours=True)} – "
                         f"{format_ts(end * 1000, force_hours=True)}")
            parts.append("")
        ts = format_ts(seg.start_ms, force_hours=True)
        if show_labels and seg.speaker:
            parts.append(f"**[{ts}] 发言人 {seg.speaker}：** {seg.text}")
        else:
            parts.append(f"**[{ts}]** {seg.text}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def build_chunks(transcript: Transcript, meta: DocMeta, doc_id: str) -> list[dict]:
    """把句子聚合成适合向量检索的块。"""
    chunks: list[dict] = []
    buf: list[Segment] = []
    length = 0

    def flush() -> None:
        nonlocal buf, length
        if not buf:
            return
        text = "".join(s.text for s in buf).strip()
        if not text:
            buf, length = [], 0
            return
        spk = []
        for s in buf:
            if s.speaker and s.speaker not in spk:
                spk.append(s.speaker)
        chunks.append({
            "doc_id": doc_id,
            "chunk_id": len(chunks),
            "title": meta.title,
            "source_file": meta.source_file,
            "source_url": meta.source_url,
            "start": format_ts(buf[0].start_ms, force_hours=True),
            "end": format_ts(buf[-1].end_ms or buf[-1].start_ms, force_hours=True),
            "start_ms": buf[0].start_ms,
            "end_ms": buf[-1].end_ms or buf[-1].start_ms,
            "speakers": spk,
            "text": text,
            "char_count": len(text),
        })
        buf, length = [], 0

    prev_end = None
    for seg in _meaningful_segments(transcript.segments):
        gap = (seg.start_ms - prev_end) / 1000 if prev_end is not None else 0
        # 说话人切换 或 间隔过久 或 超长 → 断块
        speaker_changed = bool(buf) and seg.speaker != buf[-1].speaker
        if buf and (length >= CHUNK_TARGET_CHARS
                    or length + len(seg.text) > CHUNK_MAX_CHARS
                    or gap > CHUNK_GAP_SECONDS
                    or speaker_changed):
            flush()
        buf.append(seg)
        length += len(seg.text)
        prev_end = seg.end_ms or seg.start_ms
    flush()
    return chunks


def write_outputs(transcript: Transcript, meta: DocMeta, outdir: Path,
                  slug: str | None = None) -> dict:
    """写 <slug>.md / <slug>.chunks.jsonl / <slug>.result.json，返回路径字典。"""
    outdir.mkdir(parents=True, exist_ok=True)
    slug = slug or slugify(meta.source_file or meta.title)
    doc_id = slug

    md_path = outdir / f"{slug}.md"
    md_path.write_text(build_markdown(transcript, meta), encoding="utf-8")

    chunks = build_chunks(transcript, meta, doc_id)
    chunk_path = outdir / f"{slug}.chunks.jsonl"
    with open(chunk_path, "w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    result_path = outdir / f"{slug}.result.json"
    result_path.write_text(json.dumps({
        # 顶层第一项，打开 JSON 时无需翻到 meta 里找总结。
        "summary": meta.ai_summary or {},
        "meta": asdict(meta),
        "duration_ms": transcript.duration_ms,
        "backend": transcript.backend,
        "model": transcript.model,
        "segments": [asdict(s) for s in transcript.segments],
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    return {"markdown": md_path, "chunks": chunk_path, "result": result_path,
            "chunk_count": len(chunks)}


def load_result(path: Path) -> tuple[Transcript, DocMeta]:
    """从已保存的 .result.json 重新生成输出（改了排版不用重新转写）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    segs = [Segment(**s) for s in data.get("segments", [])]
    tr = Transcript(segments=segs, duration_ms=data.get("duration_ms", 0),
                    model=data.get("model", ""), backend=data.get("backend", ""))
    meta_data = dict(data["meta"])
    # 兼容旧结果；新版把总结同时放在 JSON 顶层，便于直接查看。
    if data.get("summary") and not meta_data.get("ai_summary"):
        meta_data["ai_summary"] = data["summary"]
    meta = DocMeta(**meta_data)
    return tr, meta


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------
# 整库索引：给知识库一个「总目录」，并合并所有分块
# --------------------------------------------------------------------------

def build_index(outdir: Path, combined_name: str = "all.chunks.jsonl") -> dict:
    """扫描输出目录，生成 index.json 和合并后的 all.chunks.jsonl。

    知识库入库时通常需要一份清单（有哪些文档、多长、谁说过话）和一份
    可以直接喂给向量库的合并分块文件——不用自己去遍历目录拼。
    """
    outdir = Path(outdir)
    docs: list[dict] = []
    combined_path = outdir / combined_name
    total_chunks = 0

    with open(combined_path, "w", encoding="utf-8") as combined:
        for result_path in sorted(outdir.glob("*.result.json")):
            slug = result_path.name[: -len(".result.json")]
            chunks_path = outdir / f"{slug}.chunks.jsonl"
            md_path = outdir / f"{slug}.md"
            try:
                tr, meta = load_result(result_path)
            except Exception:  # 损坏的结果文件不该让整库索引失败
                continue

            chunks: list[dict] = []
            if chunks_path.exists():
                for line in chunks_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunks.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                for c in chunks:
                    combined.write(json.dumps(c, ensure_ascii=False) + "\n")

            speakers = _speakers(tr.segments)
            chars = sum(len(s.text) for s in tr.segments)
            docs.append({
                "doc_id": slug,
                "title": meta.title,
                "source_file": meta.source_file,
                "source_url": meta.source_url,
                "duration": format_ts(tr.duration_ms, force_hours=True),
                "duration_seconds": tr.duration_ms // 1000,
                "speaker_count": len(speakers),
                "single_speaker": len(speakers) == 1,
                "speakers": speakers,
                "segment_count": len(tr.segments),
                "char_count": chars,
                "chunk_count": len(chunks),
                "asr_backend": meta.asr_backend,
                "asr_model": meta.asr_model,
                "transcribed_at": meta.transcribed_at,
                "markdown": md_path.name if md_path.exists() else "",
                "chunks_file": chunks_path.name if chunks_path.exists() else "",
            })
            total_chunks += len(chunks)

    index = {
        "generated_at": now_iso(),
        "doc_count": len(docs),
        "total_chunks": total_chunks,
        "total_duration_seconds": sum(d["duration_seconds"] for d in docs),
        "total_char_count": sum(d["char_count"] for d in docs),
        "combined_chunks": combined_name if total_chunks else "",
        "docs": docs,
    }
    if not total_chunks:
        # 没有分块就别留一个空文件，免得下游以为有内容
        combined_path.unlink(missing_ok=True)
    (outdir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return index
