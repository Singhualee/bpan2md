"""实时体检：用一段几十秒的测试音频，把「上传 → 公网可达 → 转写 → 出 Markdown」
整条云端链路真跑一遍。

存在的意义：百度那半段必须用你的账号才测得了，但云端这半段（OSS 配置、
API Key、模型名、说话人分离、结果解析）完全可以在**不碰百度**的情况下
提前验证。批量跑几小时之前先花一分钟把这条路走通，能省掉大量返工。

    python run.py doctor
    python run.py doctor --audio 我的一段真实录音.mp3   # 用真实音频，能验证识别质量
"""

from __future__ import annotations

import time
from pathlib import Path

from . import http, md, media
from .asr import AsrError, Segment, make_asr
from .config import Config
from .storage import StorageError, make_storage

# 每模型单价（元/秒），用于估算；token 计费的模型无法这样估
PRICE_PER_SECOND = {
    "paraformer-v2": 0.00008,
    "paraformer-v1": 0.00008,
    "qwen3-asr-flash-filetrans": 0.00022,
    "qwen3-asr-flash": 0.00022,
    "fun-asr": 0.00022,
    "qwen-audio-3.0-asr-flash-filetrans": 0.00022,
}
TEST_SECONDS = 20

# 内部管道（ffmpeg 命令行、重试细节）不刷屏，正式步骤用 print
_silent = lambda *a, **k: None  # noqa: E731


def _make_test_audio(cfg: Config, workdir: Path) -> Path:
    """合成一段测试音频（纯音调）。

    刻意不用 TTS：这一步验证的是**链路**——能不能上传、能不能被公网拉取、
    任务能不能成功、结果能不能解析——而不是识别质量。纯音调没有语音，
    识别为空是预期结果。要验证质量，用 --audio 传一段真实录音。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "doctor_test.mp3"
    media._run([
        cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={TEST_SECONDS}",
        "-ac", cfg.audio_channels, "-ar", cfg.audio_samplerate,
        "-b:a", cfg.audio_bitrate, str(out),
    ])
    return out


def _check_public(url: str) -> tuple[bool, str]:
    """确认这个 URL 在「不带任何凭据」的情况下能取到内容。

    这一步很关键：对象存储如果没开公共读、或者签名算错了，转写服务那边
    只会给一个含糊的失败；不如在这里先说清楚到底是不是能被读到。
    """
    try:
        resp = http.get(url, headers={"Range": "bytes=0-0", "User-Agent": "bpan2md-doctor"},
                        timeout=60, log=_silent, what="检查公网可达性")
    except Exception as exc:  # noqa: BLE001
        return False, f"请求失败：{exc}"
    if resp.status_code in (200, 206):
        return True, f"HTTP {resp.status_code}，公网可以直接取到"
    if resp.status_code == 403:
        return False, ("HTTP 403 —— 对象不可匿名读取，转写服务同样读不到。"
                       "把 OSS_PUBLIC_READ 设为 true，或确认签名 URL 没过期。")
    if resp.status_code == 404:
        return False, "HTTP 404 —— 对象不存在，检查桶名 / endpoint / key"
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def run_doctor(cfg: Config, audio: str | None = None, log=print) -> int:
    print("=" * 64)
    print("bpan2md 实时体检（不接触百度网盘，只验证云端转写链路）")
    print("=" * 64)

    # 体检不碰百度，所以不要求 Cookie 配置
    problems = cfg.check(require_baidu=False)
    if problems:
        print("\n配置还不完整：")
        for p in problems:
            print("  ✗", p)
        return 1

    workdir = cfg.workdir / "doctor"
    report: list[tuple[str, str, str]] = []
    t_all = time.time()

    # ---- 1. 准备音频 ----
    if audio:
        src = Path(audio)
        if not src.exists():
            print(f"\n✗ 找不到音频文件：{src}")
            return 1
        print(f"\n[1/4] 使用你提供的音频：{src.name}")
    else:
        print(f"\n[1/4] 生成 {TEST_SECONDS} 秒测试音频（纯音调，识别不出文字属预期）")
        src = _make_test_audio(cfg, workdir)
    try:
        duration = media.probe_duration(cfg, src)
    except media.MediaError as exc:
        print(f"  ✗ ffprobe 失败：{exc}")
        return 1
    size_mb = src.stat().st_size / 1024 / 1024
    print(f"  ✓ {src.name}　{duration:.1f} 秒　{size_mb:.2f} MB")
    report.append(("1. 准备音频", "✓", f"{duration:.1f}s / {size_mb:.2f}MB"))

    # ---- 2. 上传 + 3. 公网可达（bailian 才需要）----
    public_url = ""
    if cfg.asr_backend == "bailian":
        print(f"\n[2/4] 上传到公网托管（{cfg.oss_bucket or cfg.public_base_url}）")
        t0 = time.time()
        try:
            storage = make_storage(cfg)
            key = f"{cfg.oss_prefix.strip('/')}/_doctor/{src.name}"
            public_url = storage.upload(src, key, log=_silent)
        except StorageError as exc:
            print(f"  ✗ 上传失败：{exc}")
            report.append(("2. 上传", "✗", str(exc)[:60]))
            _summary(report, t_all)
            return 1
        up_sec = time.time() - t0
        print(f"  ✓ 上传成功（{up_sec:.1f}s）")
        print(f"    {public_url[:110]}{'...' if len(public_url) > 110 else ''}")
        if getattr(storage, "public_acl_blocked", False):
            # 上传日志被静音了，这里必须自己说清楚：链路上看到的是一条带 Key 的
            # 签名链接，用户会疑惑为什么和设置里写的不一样。
            print("  ! 这个 Bucket 开启了「阻止公共访问」，不能把文件设成公共读。")
            print("    已自动改用「私有 + 临时签名链接」（上面的 URL 里带 OSSAccessKeyId 就是它）。")
            print("    对转写服务来说效果完全一样，无需任何改动。")
            print("    想以后不再看到这条提示：设置里把「音频上传后是否公开可读」改成「私有」。")
        report.append(("2. 上传", "✓", f"{up_sec:.1f}s"))

        print("\n[3/4] 检查这个 URL 是否真的能被公网读到")
        ok, why = _check_public(public_url)
        print(f"  {'✓' if ok else '✗'} {why}")
        report.append(("3. 公网可达", "✓" if ok else "✗", why[:60]))
        if not ok:
            _summary(report, t_all)
            return 1
    else:
        print("\n[2/4] 跳过上传（siliconflow 后端 multipart 直传，不需要托管）")
        print("[3/4] 跳过公网可达性检查（不适用）")
        report.append(("2. 上传", "—", "直传模式"))
        report.append(("3. 公网可达", "—", "不适用"))

    # ---- 4. 转写 ----
    print(f"\n[4/4] 调用转写（{cfg.asr_backend}）")
    t0 = time.time()
    try:
        asr = make_asr(cfg)
        tr = asr.transcribe(public_url, log=log) if cfg.asr_backend == "bailian" \
            else asr.transcribe(src, log=log)
    except AsrError as exc:
        print(f"  ✗ 转写失败：{exc}")
        report.append(("4. 转写", "✗", str(exc)[:80]))
        _summary(report, t_all)
        return 1
    asr_sec = time.time() - t0
    print(f"  ✓ 任务成功（{asr_sec:.1f}s），{len(tr.segments)} 段，{len(tr.text)} 字")
    if tr.segments:
        print(f"    首段：{tr.segments[0].text[:60]}")
    else:
        print("    （纯音调没有语音，识别为空是预期；要验证识别质量请用 --audio）")
    report.append(("4. 转写", "✓", f"{asr_sec:.1f}s / {len(tr.segments)}段"))

    # ---- 5. 出 Markdown ----
    if not tr.segments:
        tr.segments = [Segment(0, int(duration * 1000),
                               "（体检用纯音调，没有可识别的语音内容）")]
    meta = md.DocMeta(
        title="体检样例", source_file=src.name, source_url=public_url,
        duration_ms=int(duration * 1000), asr_backend=cfg.asr_backend,
        asr_model=(cfg.bailian_model if cfg.asr_backend == "bailian"
                   else cfg.siliconflow_model),
        diarization=cfg.bailian_diarization and cfg.asr_backend == "bailian",
        transcribed_at=md.now_iso(),
        extra={"note": "doctor 体检产出，可删除"},
    )
    # 体检产物单独放一个子目录，免得混进知识库正文里被 index 扫到
    doctor_out = cfg.outdir / "_doctor"
    outs = md.write_outputs(tr, meta, doctor_out, slug="体检样例")
    print(f"\n  ✓ {outs['markdown'].name} / {outs['chunks'].name}"
          f"（{outs['chunk_count']} 块）")
    report.append(("5. 输出 Markdown", "✓", f"{outs['chunk_count']} 块"))

    _summary(report, t_all)
    _cost_note(cfg, duration)
    print("\n云端链路已打通。接下来才是百度那半段：")
    print('  python run.py inspect "<分享链接>"')
    print('  python run.py speedtest "<分享链接>"')
    return 0


def _summary(report: list[tuple[str, str, str]], t_all: float) -> None:
    print("\n" + "-" * 64)
    print(f"{'步骤':<18}{'结果':<6}说明")
    print("-" * 64)
    for name, status, note in report:
        print(f"{name:<18}{status:<6}{note}")
    print("-" * 64)
    print(f"总耗时 {time.time() - t_all:.1f}s")


def _cost_note(cfg: Config, duration: float) -> None:
    model = cfg.bailian_model if cfg.asr_backend == "bailian" else cfg.siliconflow_model
    per_sec = PRICE_PER_SECOND.get(model)
    print("\n花费估算：")
    if cfg.asr_backend == "siliconflow":
        print(f"  {model}：官方价格页显示免费（以你的账单为准）")
    elif per_sec:
        print(f"  {model}：{per_sec} 元/秒 ≈ {per_sec*3600:.2f} 元/小时")
        print(f"  本次体检音频 {duration:.0f} 秒 ≈ {per_sec*duration:.4f} 元")
        print(f"  参考：10 小时素材 ≈ {per_sec*36000:.2f} 元")
    else:
        print(f"  {model}：按 token 计费，无法按时长估算，以账单为准")
    print("  免费额度：百炼录音文件识别 36000 秒（10 小时）")
