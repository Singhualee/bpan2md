"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import baidu, md
from .config import Config
from .doctor import PRICE_PER_SECOND, run_doctor
from .pipeline import (Pipeline, clean_workdir, durations_from_files,
                       human_hours, human_seconds, human_size, parse_links_file,
                       run_round, run_round_local, summarize, supervise)
from .preflight import run_preflight


def estimate_cost(cfg: Config, seconds: float) -> str:
    """按当前后端/模型粗估转写花费。"""
    if cfg.asr_backend == "siliconflow":
        return f"{cfg.siliconflow_model} 目前免费"
    per_sec = PRICE_PER_SECOND.get(cfg.bailian_model)
    if not per_sec:
        return f"{cfg.bailian_model} 按 token 计费，以账单为准"
    yuan = per_sec * seconds
    free_hours = 10  # 百炼录音文件识别的免费额度
    note = ""
    if seconds <= free_hours * 3600:
        note = f"（在 {free_hours} 小时免费额度内，可能不花钱）"
    return f"{yuan:.2f} 元{note}"


def _print_config(cfg: Config) -> None:
    print("当前配置：")
    for k, v in cfg.redacted().items():
        print(f"  {k}: {v}")


def cmd_check(args, cfg: Config) -> int:
    _print_config(cfg)
    problems = cfg.check()
    print()
    if problems:
        print("发现以下问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("✓ 配置看起来没问题")

    # 顺手检查外部依赖
    import shutil
    for tool in (cfg.ffmpeg, cfg.ffprobe):
        print(f"  {'✓' if shutil.which(tool) else '✗'} {tool}"
              + ("" if shutil.which(tool) else "  ← 没找到，请装 ffmpeg 并加入 PATH"))
    if cfg.asr_backend == "bailian":
        try:
            import requests  # noqa: F401
            print("  ✓ requests 已安装")
        except ImportError:
            print("  ✗ 缺 requests：pip install requests")
    return 0


def cmd_inspect(args, cfg: Config) -> int:
    info = baidu.inspect_share(cfg, args.url, args.pwd)
    print(json.dumps(info, ensure_ascii=False, indent=1))

    # 顺带把成本摊开：分享元数据里有时长，不用下载就能估
    seconds, unknown = durations_from_files(info["files"])
    print()
    if seconds:
        if cfg.asr_backend == "bailian":
            from . import models as _models
            print("预计转写：" + _models.format_cost(cfg.bailian_model, seconds, unknown))
        else:
            print(f"预计转写：{human_hours(seconds/3600)} 音频，"
                  f"{cfg.siliconflow_model} 目前免费")
        print("（想更省可换模型，参见 README 的成本对照表；"
              "python run.py check 会显示当前模型的价格）")
    else:
        print("分享里没有时长信息，无法预估转写费用（文件夹分享常见）")
    return 0


def cmd_speedtest(args, cfg: Config) -> int:
    print("测速需要先把文件转存到你自己的网盘（下载通道只认自己网盘里的路径）。\n")
    info = baidu.inspect_share(cfg, args.url, args.pwd)
    files = [f for f in info["files"] if not f["isdir"]]
    if not files:
        print("分享的顶层没有文件（可能是个文件夹，先 inspect 看看）")
        return 1
    target = max(files, key=lambda f: f["size"])
    print(f"拿最大的那个来测：{target['name']}  {human_size(target['size'])}\n")
    saved = baidu.save_share(cfg, args.url, args.pwd, fsids=str(target["fs_id"]))
    items = saved.get("saved") or []
    if not items:
        print("转存失败，没有拿到可下载的路径")
        return 1
    result = baidu.measure_speed(cfg, items, sample_chunks=args.chunks)

    # 关键：把「整份分享」的耗时算出来，让用户在下决心之前看到真实代价
    total = sum(f["size"] for f in info["files"])
    kbps = result["kbps"] or 0.001
    eta_h = total / (kbps * 1024) / 3600
    print()
    print("-" * 64)
    print(f"这个分享总计 {human_size(total)}（{len(info['files'])} 个顶层条目）")
    print(f"按实测 {result['kbps']} KB/s，全部下载完约需 {human_hours(eta_h)}")
    if kbps < 300:
        print("→ 这是百度非会员的账号级限速。换机器/多线程都没用；")
        print("  要快只有开 SVIP（实测能到 7-10 MB/s），或者接受挂机跑。")
    print("-" * 64)

    Pipeline(cfg).record_speed(args.url, kbps)
    print(f"已把这个速度记到状态文件，之后 run 时会自动给出耗时预估。")
    return 0


def _run_round(cfg: Config, targets: list[tuple[str, str]], args):
    """跑一轮。真正的实现在 pipeline.run_round，网页界面调的是同一个函数。"""
    return run_round(cfg, targets, only=args.only, limit=args.limit,
                     dry_run=args.dry_run, speakers=args.speakers, log=print)


def _report(cfg: Config, totals: dict, failures: list[str], aborted: bool,
            supervised: bool = False, dry_run: bool = False) -> None:
    print(f"\n{'='*70}\n批量汇总\n{'='*70}")
    if dry_run:
        # 「只看计划」下说"待续 N 个"会让人以为真有 N 个任务排队，其实什么都没做
        print("  「只看计划」：只做了识别和估算，没有下载、没有上传、没有花钱。")
        print(f"  计划处理 {totals.get('total', 0)} 个文件"
              f"（每个的花费估算写在上面各条「预计转写」里）")
        print("  确认没问题，去掉 --dry-run 再跑一次就会真的开始。")
        return
    print(f"  文件：{totals.get('total', 0)} 个　成功 {totals.get('done', 0)}　"
          f"失败 {totals.get('failed', 0)}　待续 {totals.get('pending', 0)}")
    print(f"  已下载 {human_size(totals.get('done_bytes', 0))}，"
          f"音频总时长 {human_seconds(totals.get('audio_seconds', 0.0))}")
    if totals.get("audio_seconds"):
        print(f"  转写花费估算：约 {estimate_cost(cfg, totals['audio_seconds'])}")
    if failures:
        print(f"\n  有 {len(failures)} 项失败：")
        for f in failures[:20]:
            print(f"    · {f}")
        if len(failures) > 20:
            print(f"    …… 另外 {len(failures)-20} 项")
        if aborted:
            print("\n  批量已中止：继续跑只会一个个失败。按上面的提示处理好之后，"
                  "\n  重跑同一条命令会从断点续上（已下载的不重下，已转写的不重花钱）。")
        elif not supervised:
            print("\n  重跑同一条命令会自动续上已完成的进度（失败的会重试）。")


def cmd_run(args, cfg: Config) -> int:
    # 先做配置前置检查：否则缺 Key 会表现为「批量里有一项失败」，很误导
    problems = cfg.check()
    if problems:
        print("配置还没就绪：")
        for p in problems:
            print("  ✗", p)
        print("\n填好 .env 之后再用 python run.py check 确认一遍。")
        return 2

    targets: list[tuple[str, str]] = []
    if args.links:
        targets = parse_links_file(Path(args.links))
        print(f"从 {args.links} 读到 {len(targets)} 个链接")
    if args.url:
        targets.insert(0, (args.url, args.pwd or ""))
    if not targets:
        print("没给链接。用法：run <分享链接> 或 run --links links.txt")
        return 2

    env_file = Path(args.env) if args.env else None

    if args.retry_hours and not args.dry_run:
        holder: dict = {}

        def one_round(cfg_now: Config):
            totals, failures, aborted, states = _run_round(cfg_now, targets, args)
            holder.update(totals=totals, failures=failures, aborted=aborted)
            return states

        print(f"监督模式：最多跑 {args.retry_hours:g} 小时，"
              f"每轮间隔 {args.retry_interval:g} 分钟，每轮重新读一次 .env。")
        result = supervise(
            one_round,
            # 每轮重新加载配置：挂机期间更新 Cookie 就能自动接上，不用重启
            lambda: Config.load(env_file),
            hours=args.retry_hours,
            interval_sec=args.retry_interval * 60,
        )
        cfg = Config.load(env_file)
        _report(cfg, holder.get("totals", {}), holder.get("failures", []),
                holder.get("aborted", False), supervised=True)
        if not result["finished"]:
            print(f"\n共跑了 {result['rounds']} 轮仍未全部完成，重跑即可续上。")
        failed = len(holder.get("failures") or [])
    else:
        totals, failures, aborted, _states = _run_round(cfg, targets, args)
        _report(cfg, totals, failures, aborted, dry_run=args.dry_run)
        failed = len(failures)

    print(f"\n输出目录：{cfg.outdir}")
    if not args.dry_run:
        print("跑完记得执行一次：python run.py index")
    return 1 if failed else 0


def cmd_local(args, cfg: Config) -> int:
    """直接转本机上的音视频文件（不经过百度网盘）。

    存在的理由：百度网盘**客户端**的下载速度往往比脚本走网页通道快一个
    数量级，而这条路完全不碰网盘——不需要 Cookie，也不占网盘中转目录。
    """
    from . import media as _media
    problems = cfg.check(require_baidu=False)
    if problems:
        print("云端转写还没配好：")
        for p in problems:
            print("  ✗", p)
        print("\n（这条路不需要百度 Cookie，但转写服务本身还是要配的。）")
        return 2

    paths: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if p.is_dir():
            found, truncated = _media.find_media(p, deep=not args.shallow)
            print(f"{p} → 找到 {len(found)} 个音视频"
                  + ("（太多了，只取了前一批）" if truncated else ""))
            paths.extend(found)
        else:
            paths.append(p)
    if not paths:
        print("没找到可处理的音视频文件。可以给文件，也可以给文件夹。")
        return 2
    if args.limit:
        paths = paths[:args.limit]

    totals, failures, aborted, _states = run_round_local(
        cfg, paths, dry_run=args.dry_run, speakers=args.speakers, log=print)
    _report(cfg, totals, failures, aborted, dry_run=args.dry_run)
    print(f"\n输出目录：{cfg.outdir}")
    if not args.dry_run:
        print("跑完记得执行一次：python run.py index")
    return 1 if failures else 0


def cmd_render(args, cfg: Config) -> int:
    """从已保存的 result.json 重新生成 Markdown（不调用 AI、不产生费用）。"""
    result = Path(args.result)
    tr, meta = md.load_result(result)
    # 用 result.json 自己的文件名当 slug：它才是当初写出去的那个名字。
    # 换成 source_file 重算的话，嵌套目录下的文件会被写成另一个新文件。
    slug = result.name
    if slug.endswith(".result.json"):
        slug = slug[: -len(".result.json")]
    outs = md.write_outputs(tr, meta, cfg.outdir, slug=slug)
    print(f"已生成 {outs['markdown']}（{outs['chunk_count']} 块）")
    return 0


def cmd_web(args, cfg: Config) -> int:
    """启动本地网页界面（给不写命令行的人用）。"""
    from .web.server import serve
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_preflight(args, cfg: Config) -> int:
    return run_preflight(cfg, log=print)


def cmd_doctor(args, cfg: Config) -> int:
    return run_doctor(cfg, audio=args.audio, log=print)


def cmd_index(args, cfg: Config) -> int:
    """扫描输出目录，生成知识库用的总目录和合并分块文件。"""
    index = md.build_index(cfg.outdir)
    if not index["doc_count"]:
        print(f"{cfg.outdir} 里还没有转写结果。先跑 run 命令。")
        return 1
    hours = index["total_duration_seconds"] / 3600
    print(f"已索引 {index['doc_count']} 篇，共 {index['total_chunks']} 个分块")
    print(f"  总时长 {hours:.2f} 小时，总字数 {index['total_char_count']:,}")
    print(f"  {cfg.outdir / 'index.json'}　← 文档清单")
    print(f"  {cfg.outdir / index['combined_chunks']}　← 合并后的分块，直接喂向量库")
    for d in index["docs"][:10]:
        print(f"    · {d['title']}　{d['duration']}　{d['chunk_count']} 块"
              f"　{d['speaker_count']} 位说话人")
    if index["doc_count"] > 10:
        print(f"    …… 另外 {index['doc_count'] - 10} 篇见 index.json")
    return 0


def cmd_clean(args, cfg: Config) -> int:
    """清理中间产物（音频、切片）。raw/ 里的原视频默认保留。"""
    clean_workdir(cfg, raw=args.raw, uploads=args.uploads, log=print)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bpan2md",
        description="百度网盘分享链接 → 云端转写 → 带元数据的 Markdown（供知识库使用）",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="检查配置和依赖")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("web",
                       help="打开网页界面（推荐：不用敲命令，浏览器里点按钮）")
    p.add_argument("--port", type=int, default=8765, help="端口，默认 8765")
    p.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    p.set_defaults(fn=cmd_web)

    p = sub.add_parser("preflight",
                       help="上线前预检：域名/端点连通性、OSS 桶名与地域（不花钱、不用真 Key）")
    p.set_defaults(fn=cmd_preflight)

    p = sub.add_parser("doctor",
                       help="实时体检：用测试音频验证「上传→公网可达→转写→出稿」整条云端链路")
    p.add_argument("--audio", default=None,
                   help="用一段你自己的音频来体检（能顺带验证识别质量）")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("index", help="为输出目录生成知识库总目录 + 合并分块文件")
    p.set_defaults(fn=cmd_index)

    p = sub.add_parser("inspect", help="列出分享里的文件")
    p.add_argument("url")
    p.add_argument("--pwd", default=None, help="提取码")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("speedtest", help="实测这个账号当前的下载速度，估算整条流水线耗时")
    p.add_argument("url")
    p.add_argument("--pwd", default=None)
    p.add_argument("--chunks", type=int, default=4, help="抽样块数（每块 4MB）")
    p.set_defaults(fn=cmd_speedtest)

    p = sub.add_parser("run", help="跑完整流程")
    p.add_argument("url", nargs="?", help="百度网盘分享链接")
    p.add_argument("--pwd", default=None)
    p.add_argument("--links", default=None, help="每行一个分享链接的文件")
    p.add_argument("--only", default=None, help="只处理名字包含这个字符串的")
    p.add_argument("--limit", type=int, default=None,
                   help="只处理前 N 个顶层条目（文件夹算一个）")
    p.add_argument("--dry-run", action="store_true",
                   help="只解析分享、算出总大小和耗时预估，不下载也不花钱")
    p.add_argument("--speakers", choices=["auto", "single", "multi"], default="auto",
                   help="说话人处理：auto=按转写结果自动判定（默认，单人内容不加"
                        "「发言人 N：」前缀）；single=强制关掉分离（输出更干净、"
                        "单段上限不必收紧到 2 小时、切片更少）；multi=强制开启分离")
    p.add_argument("--retry-hours", type=float, default=0.0,
                   help="监督模式：在这么多小时内反复重试，直到全部完成。"
                        "每轮重新读 .env，所以挂机期间更新 Cookie 会自动生效")
    p.add_argument("--retry-interval", type=float, default=10.0,
                   help="监督模式下每轮之间的间隔（分钟），默认 10")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("local",
                       help="直接转本机上的文件（不经过百度网盘，不需要 Cookie）")
    p.add_argument("paths", nargs="+", help="文件或文件夹（文件夹会被扫描）")
    p.add_argument("--shallow", action="store_true",
                   help="扫描文件夹时不进子目录")
    p.add_argument("--limit", type=int, default=None, help="只处理前 N 个")
    p.add_argument("--dry-run", action="store_true",
                   help="只读时长、估算花费，不抽音频、不上传、不花钱")
    p.add_argument("--speakers", choices=["auto", "single", "multi"], default="auto")
    p.set_defaults(fn=cmd_local)

    p = sub.add_parser("render", help="由 result.json 重新生成 Markdown")
    p.add_argument("result")
    p.set_defaults(fn=cmd_render)

    p = sub.add_parser("clean", help="清理中间产物，把磁盘空间还回去")
    p.add_argument("--raw", action="store_true",
                   help="连下载的原视频一起删（下次重跑要重新下载）")
    p.add_argument("--uploads", action="store_true",
                   help="连网页上传进来的原件一起删（那是你自己的文件，删前想清楚）")
    p.set_defaults(fn=cmd_clean)

    ap.add_argument("--env", default=None, help="指定 .env 路径（默认 ./bpan2md/.env 或 ./.env）")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    cfg = Config.load(Path(args.env) if args.env else None)
    try:
        return args.fn(args, cfg)
    except baidu.BaiduError as exc:
        print(f"百度网盘出错：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。重跑同一命令会自动续上进度。")
        return 130
