"""转写模型的「能力与价格」登记表。

为什么要单独建一张表：这些属性分散在阿里云的好几份文档里，而且**互相矛盾**。
实测核对后的事实（2026-10，官方文档原文）：

1. 模型能力表里 `paraformer-v2` 那一行写的是
   「HTTP 录音文件识别，**支持说话人分离**，中、英、日、韩、德、法、俄」；
2. 使用指南里「以下配置及示例适用于：Qwen-Audio-3.x-ASR-Flash-Filetrans、
   Fun-ASR 和 **Paraformer 系列模型**」，然后给出的参数是 `diarization_enabled`；
3. 但另一份「选型」页的推荐段落只提了 qwen-audio-3.1-* 和 fun-asr，
   容易让人以为 paraformer 不支持——**本项目早先的说明文档就错在这里**。

结论：**最便宜的 paraformer-v2（0.288 元/小时）本身就支持说话人分离，
而且计费只按音频秒数，分离不是加价项。** 所以"按单/多说话人切换 API 以省钱"
在这条路上并不成立。

另外两个容易踩的点，也收在这张表里：

- **参数名不统一**：`qwen-audio-3.1-asr-flash`（同步版）要 `speaker_diarization_enabled`，
  而 filetrans / fun-asr / paraformer 用 `diarization_enabled`。传错了不会报错，
  只是**静默地没有分离效果**——最坏的一种失败。
- **时长上限**：启用分离时官方建议 ≤2 小时；fun-asr-flash 这类同步小模型只有 5 分钟。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    name: str
    price_per_second: float | None   # None 表示按 token 计费，无法按时长估算
    diarization: bool
    diarization_param: str | None    # 开启分离时的参数名；不支持则为 None
    needs_public_url: bool           # True 走 filetrans 异步接口
    max_seconds: int                 # 0 = 未注明
    max_bytes: int
    # 官方示例里 language_hints 只出现在 qwen-audio 系列上，其它模型不传更稳妥
    language_hints: bool = False
    note: str = ""


GB = 1024 ** 3

# 顺序即推荐顺序（便宜的在前）
CATALOG: dict[str, Spec] = {
    "paraformer-v2": Spec(
        "paraformer-v2", 0.00008, True, "diarization_enabled", True,
        12 * 3600, 2 * GB, language_hints=False,
        note="最便宜，且支持说话人分离；中英日韩德法俄",
    ),
    "paraformer-v1": Spec(
        "paraformer-v1", 0.00008, True, "diarization_enabled", True,
        12 * 3600, 2 * GB, note="中文普通话、英文",
    ),
    "paraformer-mtl-v1": Spec(
        "paraformer-mtl-v1", 0.00008, True, "diarization_enabled", True,
        12 * 3600, 2 * GB, note="多语种",
    ),
    "qwen3-asr-flash-filetrans": Spec(
        "qwen3-asr-flash-filetrans", 0.00022, False, None, True,
        12 * 3600, 2 * GB, language_hints=True,
        note="不支持说话人分离，但固定开启情感识别",
    ),
    "fun-asr": Spec(
        "fun-asr", 0.00022, True, "diarization_enabled", True,
        12 * 3600, 2 * GB, note="多语种及方言，支持分离",
    ),
    "fun-asr-mtl": Spec(
        "fun-asr-mtl", 0.00022, True, "diarization_enabled", True,
        12 * 3600, 2 * GB, note="多语种及方言，支持分离",
    ),
    "fun-asr-flash-2026-06-15": Spec(
        "fun-asr-flash-2026-06-15", 0.00022, False, None, False,
        5 * 60, 2 * GB, note="同步小模型，仅 5 分钟，不适合长音频",
    ),
    "qwen-audio-3.0-asr-flash-filetrans": Spec(
        "qwen-audio-3.0-asr-flash-filetrans", 0.00022, True,
        "diarization_enabled", True, 12 * 3600, 2 * GB, language_hints=True,
        note="支持分离",
    ),
    "qwen-audio-3.1-asr-flash-filetrans": Spec(
        "qwen-audio-3.1-asr-flash-filetrans", None, True,
        "diarization_enabled", True, 12 * 3600, 2 * GB, language_hints=True,
        note="按 token 计费；官方推荐的分离模型",
    ),
    "qwen-audio-3.1-asr-flash": Spec(
        "qwen-audio-3.1-asr-flash", None, True,
        "speaker_diarization_enabled", False, 5 * 60, 2 * GB,
        language_hints=True,
        note="同步版，参数名与 filetrans 不同；仅 5 分钟",
    ),
}

# 官方建议：开启说话人分离时单段不超过 2 小时
DIARIZATION_MAX_SECONDS = 2 * 3600


def lookup(model: str) -> Spec | None:
    """查模型登记表；未知模型返回 None（新模型上线时不该直接报错）。"""
    if model in CATALOG:
        return CATALOG[model]
    # 带日期后缀的版本号（如 fun-asr-2025-11-07）归并到主名
    for name, spec in CATALOG.items():
        if model.startswith(name + "-") and not name[-1].isdigit():
            return spec
    return None


def diarization_supported(model: str) -> bool:
    spec = lookup(model)
    return bool(spec and spec.diarization)


def price_per_hour(model: str) -> float | None:
    spec = lookup(model)
    if not spec or spec.price_per_second is None:
        return None
    return spec.price_per_second * 3600


def describe(model: str) -> str:
    """一句话说明这个模型的价格和能力，用于在日志/自检里直接展示。"""
    spec = lookup(model)
    if not spec:
        return f"{model}（不在已知登记表里，价格和能力请以官方文档为准）"
    if spec.price_per_second is None:
        price = "按 token 计费"
    else:
        price = f"{spec.price_per_second*3600:.3f} 元/小时"
    dia = "支持说话人分离" if spec.diarization else "不支持说话人分离"
    size = ""
    if spec.max_seconds:
        size = f"；单段 ≤{spec.max_seconds//3600} 小时" if spec.max_seconds >= 3600 \
            else f"；单段 ≤{spec.max_seconds//60} 分钟"
    return f"{model}：{price}，{dia}{size}"


def cheapest_with_diarization() -> Spec:
    """支持分离的模型里最便宜的那个——省钱的默认选择。"""
    cands = [s for s in CATALOG.values()
             if s.diarization and s.price_per_second is not None and s.needs_public_url]
    return min(cands, key=lambda s: s.price_per_second or 9e9)


def estimate_cost_yuan(model: str, seconds: float) -> float | None:
    """按时长估算费用；按 token 计费的模型返回 None（无法按时长推）。"""
    p = price_per_hour(model)
    if p is None or seconds <= 0:
        return None
    return p * seconds / 3600


def format_cost(model: str, seconds: float, unknown_count: int = 0) -> str:
    """一句话成本预估，用在下载之前——先看清价钱再决定要不要跑。"""
    yuan = estimate_cost_yuan(model, seconds)
    if yuan is None:
        return f"{model} 按 token 计费，无法按时长预估，以账单为准"
    hours = seconds / 3600
    tail = ""
    if unknown_count:
        tail = f"（另有 {unknown_count} 项时长未知，未计入）"
    note = "，在每月 10 小时免费额度内" if hours <= 10 else ""
    return f"{hours:.2f} 小时音频 ≈ {yuan:.2f} 元（{model}）{note}{tail}"
