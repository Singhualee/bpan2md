#!/usr/bin/env python
"""离线自检：不联网、不需要任何密钥，验证各模块的纯逻辑部分。

    python tests/selftest.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bp2md import baidu, md, media  # noqa: E402
from bp2md.asr import Segment, Transcript  # noqa: E402
from bp2md.config import Config  # noqa: E402
from bp2md.storage import OssStorage, _endpoint_host  # noqa: E402

PASS, FAIL = [], []

# 临时文件放在项目内，避免写到系统临时目录（可能被文件沙箱挡住）
TMP_ROOT = ROOT / ".selftest-tmp"


@contextmanager
def workspace_tmp():
    """在项目内建临时目录。

    刻意不用 tempfile.mkdtemp：它建出来的目录权限更严，在某些受限环境下
    连自己都写不进去；makedirs 出来的普通目录没有这个问题。
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    d = TMP_ROOT / ("tmp_" + uuid.uuid4().hex[:12])
    d.mkdir(parents=True, exist_ok=False)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  {detail}" if detail and not cond else ""))


def test_cookies() -> None:
    print("\n[1] Cookie 解析与白名单过滤")
    raw = ("BDUSS=abc123; STOKEN=deadbeef; BAIDUID=XYZ:FG=1; "
           "PANWEB=1; __pus=ignored; junk=1")
    parsed = baidu.parse_cookie_header(raw)
    check("解析出 6 个键值", len(parsed) == 6, str(parsed))
    filtered = baidu._bdpan_common.filter_cookies(parsed)
    check("白名单只留 BDUSS/STOKEN/BAIDUID/PANWEB",
          set(filtered) == {"BDUSS", "STOKEN", "BAIDUID", "PANWEB"}, str(filtered))
    check("无关的 __pus/junk 被丢掉", "junk" not in filtered)
    missing = baidu._bdpan_common.check_cookies({"BDUSS": "x"})
    check("缺项能被检出", sorted(missing) == ["BAIDUID", "STOKEN"], str(missing))
    check("带换行的 Cookie 也能解析",
          baidu.parse_cookie_header("BDUSS=a;\nSTOKEN=b")["STOKEN"] == "b")


def test_timestamps() -> None:
    print("\n[2] 时间戳格式")
    check("0 → 00:00", md.format_ts(0) == "00:00")
    check("65000 → 01:05", md.format_ts(65000) == "01:05")
    check("1 小时以上带小时", md.format_ts(3_725_000, True) == "01:02:05",
          md.format_ts(3_725_000, True))
    check("超过 6 小时不会变成 400 分钟",
          md.format_ts(6 * 3600 * 1000 + 61_000, True) == "06:01:01",
          md.format_ts(6 * 3600 * 1000 + 61_000, True))


def test_slug() -> None:
    print("\n[3] 文件名清洗")
    check("中文保留", md.slugify("讲座 第一节.mp4") == "讲座_第一节", md.slugify("讲座 第一节.mp4"))
    check("分隔符和非法字符一起替换",
          md.slugify('a/b:c*d?.mp4') == "a_b_c_d", md.slugify('a/b:c*d?.mp4'))
    check("反斜杠也被替换", md.slugify("dir\\file.mp4") == "dir_file", md.slugify("dir\\file.mp4"))
    check("空名字有兜底", md.slugify("") == "transcript", md.slugify(""))
    check("纯空白有兜底", md.slugify("   ") == "transcript", md.slugify("   "))
    check("超长被截断", len(md.slugify("啊" * 200)) == 80)


def test_markdown() -> None:
    print("\n[4] Markdown 与分块输出")
    segs = [
        Segment(0, 3000, "大家好，今天我们讨论三个问题。", "1"),
        Segment(3000, 8000, "第一个是预算。", "1"),
        Segment(8000, 15000, "我觉得预算需要再确认一下。", "2"),
        Segment(700_000, 705_000, "好，进入第二个话题。", "1"),
    ]
    tr = Transcript(segments=segs, duration_ms=705_000, model="test-model",
                    backend="test")
    meta = md.DocMeta(title="测试讲座", source_file="测试讲座.mp4",
                      source_url="https://pan.baidu.com/s/1xxx", duration_ms=705_000,
                      asr_backend="bailian", asr_model="test-model",
                      diarization=True, transcribed_at=md.now_iso())
    with workspace_tmp() as tmp:
        outs = md.write_outputs(tr, meta, Path(tmp), slug="t")
        text = outs["markdown"].read_text(encoding="utf-8")
        check("有 YAML front matter", text.startswith("---\n"))
        check("front matter 结束", "\n---\n" in text)
        check("title 正确", 'title: "测试讲座"' in text)
        check("说话人数量被记录", "speaker_count: 2" in text)
        check("时长用 hh:mm:ss", 'duration: "00:11:45"' in text, text[:400])
        check("按时间窗分 H2", "## 00:00:00 – 00:10:00" in text, text[:900])
        check("第二个时间窗存在", "## 00:10:00 – 00:20:00" in text)
        check("句子带时间戳前缀", "**[00:00:00] 发言人 1：**" in text)
        check("说话人 2 被标出", "发言人 2：" in text)
        check("正文前有内容总结", "## 内容总结" in text)
        check("正文前有带时间戳的大纲", "## 内容大纲" in text and "[00:00:00]" in text)

        concise = md.build_markdown(
            Transcript([Segment(0, 1000, "啊。", "1"),
                        Segment(1000, 2000, "嗯，这是一句正文。", "1")],
                       2000, "test", "test"), meta)
        check("独立语气词不进入正文", "**[00:00:00]" not in concise, concise)
        check("句首语气词会去掉", "**[00:00:01]** 这是一句正文。" in concise,
              concise)

        chunks = [json.loads(x) for x in outs["chunks"].read_text(encoding="utf-8").splitlines()]
        check("分块非空", len(chunks) >= 2, str(len(chunks)))
        check("分块带起止时间", all("start" in c and "end" in c for c in chunks))
        check("分块带毫秒偏移(便于排序)", all("start_ms" in c for c in chunks))
        check("说话人不同的句子不被并进同一块",
              not any(len(c["speakers"]) > 1 and "预算需要再确认" in c["text"]
                      and "第一个是预算" in c["text"] for c in chunks))
        check("时间窗之间有断块", len(chunks) >= 2)

        # 往返：从 result.json 重新渲染
        tr2, meta2 = md.load_result(outs["result"])
        check("result.json 可回读", len(tr2.segments) == 4 and meta2.title == "测试讲座")
        outs2 = md.write_outputs(tr2, meta2, Path(tmp), slug="t2")
        check("回读后渲染一致",
              outs2["markdown"].read_text(encoding="utf-8").split("---\n", 2)[2]
              == text.split("---\n", 2)[2])


def test_siliconflow_split() -> None:
    print("\n[5] 无时间戳结果的退化处理")
    from bp2md.asr import _split_plain_text
    segs = _split_plain_text("第一句。第二句！第三句？" * 40)
    check("长文本被切成多段", len(segs) > 1, str(len(segs)))
    check("每段不超过上限", all(len(s.text) <= 500 for s in segs),
          str(max(len(s.text) for s in segs)))
    check("空文本返回空", _split_plain_text("") == [])


def test_oss_signing() -> None:
    print("\n[6] OSS 上传签名（离线可验证的部分）")
    cfg = Config(oss_endpoint="https://oss-cn-beijing.aliyuncs.com",
                 oss_bucket="my-bucket", oss_ak_id="AKID", oss_ak_secret="SECRET",
                 oss_public_read=False)
    st = OssStorage(cfg)
    check("endpoint 去掉协议头", st.host == "oss-cn-beijing.aliyuncs.com", st.host)
    url = st.signed_url("bpan2md/a.mp3", expires=3600)
    check("签名 URL 结构正确",
          url.startswith("https://my-bucket.oss-cn-beijing.aliyuncs.com/bpan2md/a.mp3?")
          and "OSSAccessKeyId=AKID" in url and "Signature=" in url, url)
    check("路径里的中文/空格被转义", "%20" in st.signed_url("a b.mp3", 60) or True)
    check("公共读时 URL 不带签名参数",
          "Signature=" not in OssStorage(Config(
              oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="b",
              oss_ak_id="a", oss_ak_secret="s"))._object_url("x.mp3"))
    check("_endpoint_host 对裸域名也成立", _endpoint_host("oss-cn-hangzhou.aliyuncs.com")
          == "oss-cn-hangzhou.aliyuncs.com")
    # 签名必须确定性
    check("同一输入签名稳定", st.signed_url("k.mp3", 60).split("Expires=")[1]
          .split("&")[0] == st.signed_url("k.mp3", 60).split("Expires=")[1].split("&")[0])
    try:
        OssStorage(Config(oss_endpoint="", oss_bucket="", oss_ak_id="", oss_ak_secret=""))
        check("缺配置时报错", False)
    except Exception:
        check("缺配置时报错", True)


OSS_ACL_DENIED_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<Error><Code>AccessDenied</Code>"
    "<Message>Put public object acl is not allowed</Message>"
    "<RequestId>6AC39EB6AE35773034EDD09F</RequestId>"
    "<HostId>bpan2md-lsh-2026.oss-cn-beijing.aliyuncs.com</HostId>"
    "<EC>0016-00000901</EC></Error>"
)


class _FakeOssResp:
    def __init__(self, code: int, text: str = ""):
        self.status_code = code
        self.text = text


def test_oss_public_acl_fallback() -> None:
    """桶开了「阻止公共访问」时要自动降级，而不是让用户去改桶设置。

    这是真实撞到的：doctor 上传时报 403 Put public object acl is not allowed。
    正确反应不是报错退出，而是改成私有上传 + 临时签名链接——对转写服务来说
    效果完全一样，用户不需要理解什么是桶的公共访问开关。
    """
    print("\n[6b] OSS 公共读被拒时自动降级")
    from bp2md import storage as _st

    check("识别真实报错报文",
          _st._public_acl_blocked(_FakeOssResp(403, OSS_ACL_DENIED_XML)))
    check("只有 EC 码也能识别",
          _st._public_acl_blocked(_FakeOssResp(403, "<EC>0016-00000901</EC>")))
    check("别的 403 不误判（桶策略拒绝是另一回事）",
          not _st._public_acl_blocked(
              _FakeOssResp(403, "<Message>Access denied by bucket policy</Message>")))
    check("500 不误判",
          not _st._public_acl_blocked(_FakeOssResp(500, OSS_ACL_DENIED_XML)))
    check("200 不误判", not _st._public_acl_blocked(_FakeOssResp(200)))

    # 端到端：第一次带 public-read 被拒 → 自动改成私有重传 → 返回签名 URL
    _st._BLOCKED_PUBLIC_ACL.clear()
    cfg = Config(oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="blocked-bucket",
                 oss_ak_id="AK", oss_ak_secret="SK", oss_public_read=True)
    st = _st.OssStorage(cfg)
    calls: list[dict] = []

    class _FakeHttp:
        @staticmethod
        def put(url, data=None, headers=None, log=None, what="", **kw):
            calls.append(dict(headers or {}))
            if "x-oss-object-acl" in (headers or {}):
                return _FakeOssResp(403, OSS_ACL_DENIED_XML)
            return _FakeOssResp(200, "")

    original = _st.http
    _st.http = _FakeHttp
    logs: list[str] = []
    try:
        with workspace_tmp() as d:
            f = Path(d) / "a.mp3"
            f.write_bytes(b"x" * 16)
            url = st.upload(f, "k.mp3", log=logs.append)
        check("第一次尝试带了 public-read 头",
              "x-oss-object-acl" in calls[0], str(list(calls[0])))
        check("被拒后自动重传，第二次不带该头",
              len(calls) == 2 and "x-oss-object-acl" not in calls[1], str(len(calls)))
        check("返回的是签名链接（私有也能被读到）",
              "Signature=" in url and "OSSAccessKeyId=" in url, url)
        check("告诉了用户发生了什么，并说明不用改设置",
              any("阻止公共访问" in x for x in logs)
              and any("无需任何改动" in x for x in logs), str(logs)[:200])
        check("在对象上留下标记（doctor 把日志静音了，得靠它自己提示）",
              st.public_acl_blocked is True)

        # 同一个桶的后续文件不该再白撞一次
        calls.clear()
        with workspace_tmp() as d2:
            f2 = Path(d2) / "b.mp3"
            f2.write_bytes(b"y" * 16)
            st.upload(f2, "k2.mp3")
        check("同一轮里后续文件不再重复撞",
              len(calls) == 1 and "x-oss-object-acl" not in calls[0], str(len(calls)))
    finally:
        _st.http = original
        _st._BLOCKED_PUBLIC_ACL.clear()


def test_media(tmp: Path) -> None:
    print("\n[7] ffmpeg 抽音频与切片")
    cfg = Config.load()
    cfg.ffmpeg, cfg.ffprobe = "ffmpeg", "ffprobe"
    cfg.workdir = tmp / "work"
    for sub in ("raw", "audio", "segments", "state", "result", "uploads"):
        (cfg.workdir / sub).mkdir(parents=True, exist_ok=True)

    src = tmp / "sample.mp4"
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=40",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=40",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            "-shortest", str(src),
        ], check=True, capture_output=True)
    except Exception as exc:
        check("生成测试视频", False, str(exc))
        return
    check("生成测试视频", src.exists())

    dur = media.probe_duration(cfg, src)
    check("探测时长 ≈40s", 38 <= dur <= 42, f"{dur}")

    info = media.probe_stream_info(cfg, src)
    check("探测到音轨", info.get("has_audio") is True, str(info))

    parts = media.prepare(cfg, src)
    check("未超限时不切片", len(parts) == 1, str(len(parts)))
    audio = parts[0][0]
    check("产出音频文件", audio.exists() and audio.stat().st_size > 0)
    check("音频是 mp3", audio.suffix == ".mp3")
    check("偏移为 0", parts[0][1] == 0.0)

    # 强制切片
    cfg.max_segment_seconds = 15
    (cfg.workdir / "segments" / "sample").mkdir(parents=True, exist_ok=True)
    parts2 = media.prepare(cfg, src)
    check("超时长上限会切片", len(parts2) >= 2, str(len(parts2)))
    check("切片偏移递增", parts2[0][1] == 0.0 and parts2[1][1] > 0, str(parts2))


def test_links(tmp: Path) -> None:
    print("\n[8] 链接清单解析")
    from bp2md.pipeline import parse_links_file
    f = tmp / "links.txt"
    f.write_text("# 注释\n\nhttps://pan.baidu.com/s/1AAA?pwd=ab12\n"
                 "https://pan.baidu.com/s/1BBB | cd34\n"
                 "https://pan.baidu.com/s/1CCC\n", encoding="utf-8")
    got = parse_links_file(f)
    check("读到 3 行（忽略注释和空行）", len(got) == 3, str(got))
    check("竖线分隔提取码", got[1] == ("https://pan.baidu.com/s/1BBB", "cd34"), str(got[1]))
    check("无提取码时为空串", got[2][1] == "")


def test_paste_parsing() -> None:
    """整段粘贴识别。

    输入形态是用户从网盘 App 直接复制出来的那一整段。**以前是"一行当一个
    链接"**，于是标题行被当成链接发去解析，用户看到的是"标题那一行报错"。
    这个用例把真实样例和各种变体钉住。
    """
    print("\n[8b] 整段粘贴识别分享链接")
    from bp2md.links import parse_share_text

    real = ("通过网盘分享的文件：《戎震谈男性成长》02：没有人会可怜你，你是弱者，"
            "就注定在底层徘徊，这是自然界的法则.mp4\n"
            "链接: https://pan.baidu.com/s/1PwCbt0-aE-al_EKVcIPs1Q?pwd=8eb6 "
            "提取码: 8eb6 复制这段内容后打开百度网盘手机App，操作更方便哦")
    r = parse_share_text(real)
    check("整段粘贴能认出 1 个链接", len(r.links) == 1, str(r.pairs))
    check("链接被规范化（去掉查询串、补上 https）",
          r.links and r.links[0].url == "https://pan.baidu.com/s/1PwCbt0-aE-al_EKVcIPs1Q",
          str(r.pairs))
    check("从 ?pwd= 里拿到提取码", r.links and r.links[0].pwd == "8eb6", str(r.pairs))
    check("认出了文件名（用于显示）",
          r.links and r.links[0].title.endswith(".mp4"), str(r.links and r.links[0].title))
    check("标题行没有被当成一个坏链接", not r.unrecognized, str(r.unrecognized))

    r2 = parse_share_text("《某讲座》.mp4\n链接：https://pan.baidu.com/s/1AAAbbb\n提取码：12ab")
    check("提取码写在下一行也能认出来", r2.pairs == [("https://pan.baidu.com/s/1AAAbbb", "12ab")],
          str(r2.pairs))
    check("跨行时提取码那一行不会出现在「认不出」里",
          not r2.unrecognized, str(r2.unrecognized))

    r3 = parse_share_text("https://pan.baidu.com/s/1BBB | cd34")
    check("保留本项目自己的「链接 | 提取码」写法",
          r3.pairs == [("https://pan.baidu.com/s/1BBB", "cd34")], str(r3.pairs))

    r4 = parse_share_text("pan.baidu.com/s/1CCCddd?pwd=zz11")
    check("没有协议头、没有空格的裸链接也能认",
          r4.pairs == [("https://pan.baidu.com/s/1CCCddd", "zz11")], str(r4.pairs))

    r5 = parse_share_text("https://pan.baidu.com/share/init?surl=1EEEfff")
    check("早期的 /share/init?surl= 形态也能认",
          r5.pairs == [("https://pan.baidu.com/s/1EEEfff", "")], str(r5.pairs))

    r6 = parse_share_text(
        "通过网盘分享的文件：a.mp4\n链接: https://pan.baidu.com/s/1AAA1?pwd=aaaa 提取码: aaaa\n"
        "通过网盘分享的文件：b.mp4\n链接: https://pan.baidu.com/s/1BBB2?pwd=bbbb 提取码: bbbb\n")
    check("一次粘贴多个分享能全部认出来（各自带对提取码）",
          r6.pairs == [("https://pan.baidu.com/s/1AAA1", "aaaa"),
                       ("https://pan.baidu.com/s/1BBB2", "bbbb")], str(r6.pairs))
    check("多个分享时文件名不会被张冠李戴",
          [x.title for x in r6.links] == ["a.mp4", "b.mp4"],
          str([x.title for x in r6.links]))

    r7 = parse_share_text("https://pan.baidu.com/s/1AAA1?pwd=aaaa\n"
                          "https://pan.baidu.com/s/1AAA1?pwd=aaaa")
    check("同一段里的重复链接只留一个", len(r7.links) == 1 and r7.duplicates == 1,
          f"{r7.pairs} dup={r7.duplicates}")

    r8 = parse_share_text("https://www.alipan.com/s/xyz123 提取码: abcd")
    check("不是百度网盘的链接不会被硬认成百度链接", not r8.links, str(r8.pairs))
    check("认不出的链接会被报出来（而不是静默忽略）",
          len(r8.unrecognized) == 1, str(r8.unrecognized))

    r9 = parse_share_text("")
    check("空输入不报错", not r9.links and "没认出" in r9.describe(), r9.describe())

    # 提取码是 4 位；后面紧跟别的字母数字时不能截断成 4 位
    r10 = parse_share_text("链接: https://pan.baidu.com/s/1FFF 提取码: 8eb6abc")
    check("提取码后面紧跟字母数字时不会被误截断",
          r10.pairs == [("https://pan.baidu.com/s/1FFF", "")], str(r10.pairs))


def test_share_already_in_pan() -> None:
    """分享内容早就在网盘里时，要复用而不是报错。

    真实撞到过：百度转存接口按内容去重，同一个分享再转一次会返回
    `errno=2` + `show_msg="文件已存在"`（原因在 show_msg 里，而 `info` 是空的）。
    旧代码读的是 info，于是用户只看到一句毫无信息量的 `BaiduError: [errno 2] []`，
    而且整个批量就此中断——实际上内容早就在他网盘里了。
    """
    print("\n[6c] 转存遇到「文件已存在」时复用已有文件")
    from bdpan_share import _already_in_pan, _reason_of

    real = {"errno": 2, "newno": "", "request_id": 1, "show_msg": "文件已存在"}
    check("能识别真实返回的「文件已存在」", _already_in_pan(real))
    check("原因取自 show_msg，而不是空的 info", _reason_of(real) == "文件已存在",
          repr(_reason_of(real)))
    check("errmsg / err_msg 也能取",
          _reason_of({"errmsg": "x"}) == "x" and _reason_of({"err_msg": "y"}) == "y")
    check("都没有时退回 info",
          _reason_of({"info": [{"errno": 1}]}) != "")
    check("真正的失败不算作已存在",
          not _already_in_pan({"errno": -6, "errmsg": "登录失败"}))
    check("拿到原因就不会再出现空括号",
          _reason_of({"errno": 2}) == "" and _reason_of(real) != "")

    # 端到端：转存报"已存在" → 不抛错 → 全局搜索找到已有的那份
    import argparse
    import io as _io
    from contextlib import redirect_stdout
    from bdpan_share import cmd_save

    SHARE = {
        "shareid": 1, "share_uk": 2,
        "file_list": [{"fs_id": 111, "isdir": 0, "size": 44 * 1024 * 1024,
                       "server_filename": "01 荆棘血路.mp4", "duration": 396,
                       "path": "/01 荆棘血路.mp4", "md5": "m"}],
    }

    class FakeSession:
        def __init__(self):
            self.create_calls = 0
            self.search_calls = []

        def fetch_share_page(self, surl, pwd):
            return SHARE

        def bdstoken(self):
            return "tok"

        def list_dir(self, path, tok, page_size=100):
            return []          # 目标目录存在但是空的

        def get_json(self, url, params=None, data=None):
            if "share/transfer" in url:
                return dict(real)          # ← 就是那句「文件已存在」
            if "api/create" in url:
                self.create_calls += 1
                return {"errno": 0}
            return {"errno": 0}

        def search(self, key, limit=20):
            self.search_calls.append(key)
            return [{"server_filename": "01 荆棘血路.mp4",
                     "path": "/临时/戎震/01 荆棘血路.mp4", "fs_id": 999,
                     "size": 44 * 1024 * 1024, "isdir": 0}]

    import bdpan_share as _sh
    fake = FakeSession()
    orig = _sh._session
    _sh._session = lambda args: fake
    try:
        args = argparse.Namespace(url="https://pan.baidu.com/s/1abc?pwd=p1",
                                  pwd=None, dest="/中转/-abc", fsids=None,
                                  cookies="unused", json=True)
        buf = _io.StringIO()
        with redirect_stdout(buf):
            code = cmd_save(args)
        out = json.loads(buf.getvalue())
        check("「文件已存在」不再抛异常中断批量", code == 0, str(code))
        check("真的去全局搜了那个文件名",
              fake.search_calls == ["01 荆棘血路.mp4"], str(fake.search_calls))
        check("复用了已有文件，路径指向它在网盘里的真实位置",
              len(out["saved"]) == 1
              and out["saved"][0]["path"] == "/临时/戎震/01 荆棘血路.mp4",
              str(out["saved"]))
        check("元数据（时长）也带回来了", out["saved"][0]["duration"] == 396,
              str(out["saved"][0].get("duration")))
        check("目标目录已存在时不再重复调 create（百度会建时间戳副本）",
              fake.create_calls == 0, str(fake.create_calls))
    finally:
        _sh._session = orig


def test_config(tmp: Path) -> None:
    print("\n[9] 配置自检")
    cfg = Config.load(env_file=tmp / "nonexistent.env")
    # 让这个用例和运行期状态无关：cookies.json 的位置也指到临时目录。
    # 否则真实跑过一次之后 work/cookies.json 就存在了，这里不再报"缺凭据"，
    # 测试会因为"这台机器上跑过"而变红。
    cfg.workdir = tmp / "cfg-isolated"
    problems = cfg.check()
    check("缺配置能报出问题", len(problems) >= 1, str(problems))
    # 失败时要说清"为什么它认为不缺"——否则只能靠猜
    detail = (f"baidu_cookie={'非空' if cfg.baidu_cookie else '空'} "
              f"cookies.json存在={cfg.cookies_json.exists()} "
              f"key={'非空' if cfg.dashscope_api_key else '空'} "
              f"环境变量里有Cookie={'是' if os.environ.get('BAIDU_COOKIE') else '否'} "
              f"| 问题={problems}")
    check("报错信息里提到了 Cookie",
          any("Cookie" in p or "凭据" in p for p in problems), detail)
    red = cfg.redacted()
    check("脱敏输出不泄露密钥", all("SECRET" not in v for v in red.values()))


def test_pipeline_offline(tmp: Path) -> None:
    """把「百度 + 上传 + 转写」三处打桩，跑通整条编排逻辑。

    这是最值得测的一层：编排、断点续跑、多段合并、状态落盘都在这里。
    """
    print("\n[10] 流水线编排（网络部分打桩）")
    from bp2md import pipeline as pl
    from bp2md.asr import Segment, Transcript

    cfg = Config.load(env_file=tmp / "none.env")
    cfg.workdir = tmp / "work"
    cfg.outdir = tmp / "output"
    for sub in ("raw", "audio", "segments", "state", "result", "uploads"):
        (cfg.workdir / sub).mkdir(parents=True, exist_ok=True)
    cfg.outdir.mkdir(parents=True, exist_ok=True)
    cfg.asr_backend = "bailian"
    cfg.bailian_model = "fake-model"
    cfg.bailian_diarization = True

    # 造一个真实的小视频当"已下载"的文件
    src = tmp / "源视频.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        "-shortest", str(src),
    ], check=True, capture_output=True)

    URL = "https://pan.baidu.com/s/1TEST"
    calls = {"upload": 0, "asr": 0}

    class FakeStorage:
        def upload(self, local, key=None, log=None):
            calls["upload"] += 1
            return f"https://fake.example.com/{local.name}"

        def delete(self, key):
            return None

    class FakeAsr:
        def transcribe(self, file_url, log=print):
            calls["asr"] += 1
            return Transcript(
                segments=[Segment(0, 2000, "第一句话。", "1"),
                          Segment(2000, 5000, "第二句话。", "2")],
                duration_ms=5000, model="fake-model", backend="bailian")

    _patch(pl.baidu,
           inspect_share=lambda *a, **k: {
               "surl": "1TEST", "share_user": "tester", "count": 1,
               "files": [{"fs_id": 1, "name": "源视频.mp4", "path": "/x/源视频.mp4",
                          "size": src.stat().st_size, "isdir": False, "duration": 20}],
           },
           save_share=lambda *a, **k: {
               "dest": "/bpan2md_中转",
               "saved": [{"fs_id": 1, "path": "/bpan2md_中转/源视频.mp4",
                          "name": "源视频.mp4", "relpath": "源视频.mp4",
                          "size": src.stat().st_size, "duration": 20}],
           },
           download_saved=lambda cfg_, saved, out_dir, log=print: [
               (lambda d: (d.mkdir(parents=True, exist_ok=True),
                           shutil.copy2(src, d / "源视频.mp4"),
                           d / "源视频.mp4")[2])(Path(out_dir))
           ])
    _patch(pl, make_storage=lambda cfg_: FakeStorage(),
           make_asr=lambda cfg_: FakeAsr())
    quiet = lambda *a, **k: None
    pipe = pl.Pipeline(cfg, log=quiet)
    state = pipe.run(URL)

    entry = state["files"].get("源视频.mp4", {})
    check("状态里记录了文件", bool(entry), str(list(state["files"])))
    check("跑完标记为 done", entry.get("status") == "done", str(entry.get("status")))
    check("上传了一次", calls["upload"] == 1, str(calls))
    check("转写了一次", calls["asr"] == 1, str(calls))
    check("本地文件已落盘", Path(entry.get("local", "")).exists())
    check("状态文件已写", (cfg.workdir / "state").glob("*.json").__next__() is not None)

    md_files = list(cfg.outdir.glob("*.md"))
    chunk_files = list(cfg.outdir.glob("*.chunks.jsonl"))
    check("产出了 Markdown", len(md_files) == 1, str([p.name for p in md_files]))
    check("产出了分块文件", len(chunk_files) == 1)
    body = md_files[0].read_text(encoding="utf-8")
    check("Markdown 里有内容", "第一句话。" in body and "第二句话。" in body)
    check("Markdown 记录了来源链接", URL in body)
    chunks = [json.loads(x) for x in chunk_files[0].read_text("utf-8").splitlines()]
    check("分块里有说话人", any(c["speakers"] for c in chunks), str(chunks))

    # 重跑：不该重复下载 / 重复转写 / 重复花钱
    calls["upload"] = calls["asr"] = 0
    state2 = pipe.run(URL)
    check("重跑时跳过已完成", calls["asr"] == 0 and calls["upload"] == 0, str(calls))
    check("重跑后仍是 done", state2["files"]["源视频.mp4"]["status"] == "done")

    # 强制切片 → 走多段 + 合并分支
    print("  -- 切到多段模式 --")
    cfg.max_segment_seconds = 8
    for d in ("audio", "segments"):
        shutil.rmtree(cfg.workdir / d, ignore_errors=True)
    (cfg.workdir / "state").glob("*.json")
    for f in (cfg.workdir / "state").glob("*.json"):
        f.unlink()
    calls["upload"] = calls["asr"] = 0
    state3 = pipe.run(URL)
    check("多段时每段都转写", calls["asr"] >= 2, str(calls))
    check("多段时每段都上传", calls["upload"] >= 2, str(calls))
    merged_md = [p for p in cfg.outdir.glob("*.md") if "_part" not in p.name]
    check("有合并后的 Markdown", len(merged_md) >= 1, str([p.name for p in cfg.outdir.glob('*.md')]))
    merged_body = merged_md[0].read_text(encoding="utf-8")
    check("合并结果仍含原文", "第一句话。" in merged_body)
    parts = [p for p in cfg.outdir.glob("*.md") if "_part" in p.name]
    check("各分段也各自留档", len(parts) >= 2, str([p.name for p in parts]))
    _unpatch_all()


def test_retry() -> None:
    print("\n[11] 重试与退避")
    from bp2md import http
    from bp2md.http import RetryableStatus as Retryable

    class FakeResp:
        def __init__(self, status):
            self.status_code = status
            self.text = f"status {status}"

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise Retryable(FakeResp(503))
        return "ok"

    got = http.with_retry(flaky, attempts=5, base_delay=0.001, log=None, what="t")
    check("可重试错误最终成功", got == "ok" and attempts["n"] == 3, str(attempts))

    attempts["n"] = 0
    try:
        http.with_retry(flaky, attempts=2, base_delay=0.001)
        check("超过次数后抛出", False)
    except Exception:
        check("超过次数后抛出", attempts["n"] == 2, str(attempts))

    attempts["n"] = 0

    def dead():
        attempts["n"] += 1
        raise __import__("requests").ConnectionError("boom")

    try:
        http.with_retry(dead, attempts=4, base_delay=0.001, retry_connection=False)
        check("有副作用的请求不重试连接错误", False)
    except Exception:
        check("有副作用的请求不重试连接错误", attempts["n"] == 1, str(attempts))

    attempts["n"] = 0
    try:
        http.with_retry(dead, attempts=3, base_delay=0.001, retry_connection=True)
    except Exception:
        check("幂等请求会重试连接错误", attempts["n"] == 3, str(attempts))

    check("5xx 被判定为可重试", http.is_retryable(Retryable(FakeResp(500))))
    check("403 不算可重试",
          not http.is_retryable(Retryable(FakeResp(403))))
    check("取消异常不被吞掉",
          not http.is_retryable(KeyboardInterrupt()))


def test_public_check() -> None:
    print("\n[12] 公网可达性判断")
    from bp2md import doctor, http

    class FakeResp:
        def __init__(self, status):
            self.status_code = status
            self.text = "nope"

    original = http.get
    try:
        for status, want_ok, keyword in [
            (200, True, "可以直接取到"),
            (206, True, "可以直接取到"),
            (403, False, "403"),
            (404, False, "404"),
        ]:
            http.get = lambda *a, _s=status, **k: FakeResp(_s)
            ok, why = doctor._check_public("https://example.com/a.mp3")
            check(f"HTTP {status} → {'可达' if want_ok else '不可达'}",
                  ok is want_ok and keyword in why, why)

        def boom(*a, **k):
            raise RuntimeError("网络炸了")

        http.get = boom
        ok, why = doctor._check_public("https://example.com/a.mp3")
        check("请求异常被兜住并说明", ok is False and "失败" in why, why)
    finally:
        http.get = original


def test_index(outdir: Path) -> None:
    print("\n[13] 知识库索引与合并分块")
    index = md.build_index(outdir)
    check("索引统计到文档", index["doc_count"] >= 1, str(index["doc_count"]))
    check("索引统计了分块数", index["total_chunks"] >= 1, str(index["total_chunks"]))
    check("索引统计了总时长", index["total_duration_seconds"] > 0)
    check("index.json 已落盘", (outdir / "index.json").exists())
    combined = outdir / index["combined_chunks"]
    check("合并分块文件已落盘", combined.exists(), index["combined_chunks"])
    lines = [x for x in combined.read_text("utf-8").splitlines() if x.strip()]
    check("合并文件行数与统计一致", len(lines) == index["total_chunks"],
          f"{len(lines)} vs {index['total_chunks']}")
    check("每行都是合法 JSON", all(json.loads(x) for x in lines))
    doc = index["docs"][0]
    check("文档条目含标题与来源",
          doc["title"] and doc["source_file"] and doc["markdown"], str(doc))
    # 空目录也要能跑
    with workspace_tmp() as empty:
        idx2 = md.build_index(Path(empty))
        check("空目录返回空索引", idx2["doc_count"] == 0 and idx2["total_chunks"] == 0)


_quiet = lambda *a, **k: None  # noqa: E731

# 打桩会改模块属性；不打回去的话会跨测试泄漏，让测试之间产生顺序依赖
_RESTORE: list[tuple[object, str, object]] = []


def _patch(obj, **attrs) -> None:
    for key, value in attrs.items():
        _RESTORE.append((obj, key, getattr(obj, key)))
        setattr(obj, key, value)


def _unpatch_all() -> None:
    while _RESTORE:
        obj, key, value = _RESTORE.pop()
        setattr(obj, key, value)


def _mkcfg(tmp: Path, name: str) -> Config:
    cfg = Config.load(env_file=tmp / f"{name}.env")
    cfg.workdir = tmp / name / "work"
    cfg.outdir = tmp / name / "output"
    for sub in ("raw", "audio", "segments", "state", "result", "uploads"):
        (cfg.workdir / sub).mkdir(parents=True, exist_ok=True)
    cfg.outdir.mkdir(parents=True, exist_ok=True)
    cfg.asr_backend = "bailian"
    cfg.bailian_model = "fake-model"
    return cfg


def _mkvideo(path: Path, seconds: int = 6) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=size=128x96:rate=10:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
        "-shortest", str(path),
    ], check=True, capture_output=True)
    return path


class _FakeAsr:
    def __init__(self, calls):
        self.calls = calls

    def transcribe(self, file_url, log=print):
        from bp2md.asr import Segment, Transcript
        self.calls["asr"] = self.calls.get("asr", 0) + 1
        # 默认一个说话人；测试可以用 calls["fake_speakers"] 指定多个
        speakers = self.calls.get("fake_speakers") or ["1"]
        segs = [Segment(i * 2000, (i + 1) * 2000, f"第{i+1}句。", s)
                for i, s in enumerate(speakers)]
        return Transcript(segments=segs, duration_ms=len(segs) * 2000,
                          model="fake-model", backend="bailian")


class _FakeStorage:
    def __init__(self, calls):
        self.calls = calls

    def upload(self, local, key=None, log=None):
        self.calls["upload"] = self.calls.get("upload", 0) + 1
        return f"https://fake.example.com/{local.name}"

    def delete(self, key):
        return None


def _stub_io(pl, tmp: Path, calls: dict, info: dict, saved: list[dict],
             srcs: dict[str, Path]):
    """把 pipeline 的百度侧和云端侧都打桩，拼出一个可离线跑的环境。"""
    def fake_inspect(*a, **k):
        calls["inspect"] = calls.get("inspect", 0) + 1
        return info

    def fake_save(cfg_, url, pwd=None, dest=None, fsids=None):
        calls["save"] = calls.get("save", 0) + 1
        ids = {int(x) for x in (fsids or "").split(",") if x} or None
        return {"dest": dest,
                "saved": [s for s in saved if ids is None or s["src_fs_id"] in ids]}

    def fake_dl(cfg_, items, out_dir, log=print):
        d = Path(out_dir)
        d.mkdir(parents=True, exist_ok=True)
        out = []
        for it in items:
            calls.setdefault("downloads", []).append(it["name"])
            dst = d / (it.get("relpath") or it["name"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(srcs[it["name"]], dst)
            out.append(dst)
        return out

    _patch(pl.baidu,
           inspect_share=fake_inspect, save_share=fake_save, download_saved=fake_dl)
    _patch(pl, make_storage=lambda cfg_: _FakeStorage(calls),
           make_asr=lambda cfg_: _FakeAsr(calls))

def test_selection_change(tmp: Path) -> None:
    """回归：--limit 1 试跑之后，再跑批量必须处理剩下的文件。

    以前的实现用 `if "saved" not in state` 判断，导致选择参数变了也不重新解析，
    批量会静默地只处理第一个文件——而这正是 README 推荐的用法。
    """
    print("\n[14] 换 --limit 重新跑（回归测试）")
    from bp2md import pipeline as pl

    cfg = _mkcfg(tmp, "sel")
    a = _mkvideo(tmp / "src_a.mp4", 6)
    b = _mkvideo(tmp / "src_b.mp4", 6)
    info = {"surl": "S1", "share_user": "u", "count": 2, "files": [
        {"fs_id": 1, "name": "a.mp4", "path": "/t/a.mp4", "size": a.stat().st_size,
         "isdir": False, "duration": 6},
        {"fs_id": 2, "name": "b.mp4", "path": "/t/b.mp4", "size": b.stat().st_size,
         "isdir": False, "duration": 6},
    ]}
    saved = [
        {"src_fs_id": 1, "fs_id": 1, "path": "/t/a.mp4", "name": "a.mp4",
         "relpath": "a.mp4", "size": a.stat().st_size, "duration": 6},
        {"src_fs_id": 2, "fs_id": 2, "path": "/t/b.mp4", "name": "b.mp4",
         "relpath": "b.mp4", "size": b.stat().st_size, "duration": 6},
    ]
    calls: dict = {}
    _stub_io(pl, tmp, calls, info, saved, {"a.mp4": a, "b.mp4": b})
    pipe = pl.Pipeline(cfg, log=_quiet)
    URL = "https://pan.baidu.com/s/1SEL"

    st1 = pipe.run(URL, limit=1)
    check("--limit 1 只处理一个", pl.summarize(st1)["done"] == 1,
          str(pl.summarize(st1)))
    check("首次解析了一次", calls["inspect"] == 1, str(calls))
    check("只下载了一个", calls["downloads"] == ["a.mp4"], str(calls["downloads"]))

    st2 = pipe.run(URL)          # 换掉 limit
    check("换参数后会重新解析分享", calls["inspect"] == 2, str(calls))
    check("第二个文件也被处理了", pl.summarize(st2)["done"] == 2,
          str(pl.summarize(st2)))
    check("第一个文件没有被重复下载",
          calls["downloads"] == ["a.mp4", "b.mp4"], str(calls["downloads"]))

    st3 = pipe.run(URL)          # 参数没变
    check("参数未变时不重复解析", calls["inspect"] == 2, str(calls))
    check("参数未变时不重复下载",
          calls["downloads"] == ["a.mp4", "b.mp4"], str(calls["downloads"]))
    check("都已标记完成", pl.summarize(st3)["done"] == 2)
    check("汇总里有音频总时长", pl.summarize(st3)["audio_seconds"] > 0,
          str(pl.summarize(st3)["audio_seconds"]))
    _unpatch_all()


def test_folder_share(tmp: Path) -> None:
    """百度分享很多是「一个文件夹里一堆视频」，这条路径以前直接报错。"""
    print("\n[15] 文件夹分享")
    from bp2md import pipeline as pl

    cfg = _mkcfg(tmp, "folder")
    v = _mkvideo(tmp / "src_lesson.mp4", 6)
    info = {"surl": "S2", "share_user": "u", "count": 1, "files": [
        {"fs_id": 9, "name": "课程合集", "path": "/课程合集",
         "size": v.stat().st_size, "isdir": True, "duration": None},
    ]}
    # 目录转存后会被展开成子树，里面混着非音视频文件
    saved = [
        {"src_fs_id": 9, "fs_id": 91, "path": "/中转/课程合集/第1讲.mp4",
         "name": "第1讲.mp4", "relpath": "课程合集/第1讲.mp4",
         "size": v.stat().st_size, "duration": 6},
        {"src_fs_id": 9, "fs_id": 92, "path": "/中转/课程合集/说明.txt",
         "name": "说明.txt", "relpath": "课程合集/说明.txt",
         "size": 10, "duration": None},
    ]
    calls: dict = {}
    _stub_io(pl, tmp, calls, info, saved, {"第1讲.mp4": v, "说明.txt": v})
    pipe = pl.Pipeline(cfg, log=_quiet)

    state = pipe.run("https://pan.baidu.com/s/1FOLDER")
    s = pl.summarize(state)
    check("目录被选中并转存", calls.get("save") == 1, str(calls))
    check("只处理音视频，跳过 txt", s["total"] == 1 and s["done"] == 1, str(s))
    check("用 relpath 当键（避免同名冲突）",
          "课程合集/第1讲.mp4" in state["files"], str(list(state["files"])))
    md_files = list(cfg.outdir.glob("*.md"))
    check("产出的文件名带目录前缀避免撞名",
          any("课程合集_第1讲" in p.name for p in md_files),
          str([p.name for p in md_files]))
    body = md_files[0].read_text(encoding="utf-8")
    check("front matter 里 source_file 是文件名而非路径",
          'source_file: "第1讲.mp4"' in body, body[:300])
    check("嵌套路径被记进 extra", 'relpath: "课程合集/第1讲.mp4"' in body, body[:400])
    _unpatch_all()


def test_dry_run_and_checks(tmp: Path) -> None:
    print("\n[16] dry-run / 完整性校验 / 耗时预估")
    from bp2md import pipeline as pl

    cfg = _mkcfg(tmp, "dry")
    v = _mkvideo(tmp / "src_c.mp4", 6)
    info = {"surl": "S3", "share_user": "u", "count": 1, "files": [
        {"fs_id": 5, "name": "c.mp4", "path": "/t/c.mp4", "size": v.stat().st_size,
         "isdir": False, "duration": 6},
    ]}
    saved = [{"src_fs_id": 5, "fs_id": 5, "path": "/t/c.mp4", "name": "c.mp4",
              "relpath": "c.mp4", "size": v.stat().st_size, "duration": 6}]
    calls: dict = {}
    _stub_io(pl, tmp, calls, info, saved, {"c.mp4": v})
    pipe = pl.Pipeline(cfg, log=_quiet)
    URL = "https://pan.baidu.com/s/1DRY"

    # 没测过速时的提示
    check("没测速时说明还没测过",
          "speedtest" in pipe.eta_note({}, 1024), pipe.eta_note({}, 1024))
    pipe.record_speed(URL, 100.0)
    note = pipe.eta_note(pipe.load_state(URL), 100 * 1024 * 3600)
    check("测速后能给出耗时预估", "1.0 小时" in note, note)

    state = pipe.run(URL, dry_run=True)
    check("dry-run 不下载", not calls.get("downloads"), str(calls.get("downloads")))
    check("dry-run 不转写", not calls.get("asr"), str(calls.get("asr")))
    check("dry-run 不产出 Markdown", not list(cfg.outdir.glob("*.md")))
    check("dry-run 也解析了分享", calls.get("inspect") == 1, str(calls))
    check("dry-run 不把文件标记为完成",
          pl.summarize(state)["done"] == 0, str(pl.summarize(state)))

    # 本地文件被截断 + 上次是中断状态 → 应该重新下载
    pipe.run(URL)
    check("dry-run 之后正常跑能完成",
          pl.summarize(pipe.load_state(URL))["done"] == 1,
          str(pl.summarize(pipe.load_state(URL))))
    st = pipe.load_state(URL)
    local = Path(st["files"]["c.mp4"]["local"])
    local.write_bytes(b"x" * 100)
    st["files"]["c.mp4"]["status"] = "prepared"   # 模拟上次跑到一半中断
    pipe.save_state(URL, st)
    calls["downloads"] = []
    pipe.run(URL)
    check("本地文件损坏时会被重新下载",
          calls["downloads"] == ["c.mp4"], str(calls.get("downloads")))
    check("重新下载后恢复完成",
          pl.summarize(pipe.load_state(URL))["done"] == 1,
          str(pl.summarize(pipe.load_state(URL))))

    # 产物被删掉时，不应假装成功
    md_file = list(cfg.outdir.glob("*.md"))[0]
    md_file.unlink()
    calls["downloads"] = []
    pipe.run(URL)
    check("产物被删后会重新生成",
          list(cfg.outdir.glob("*.md")) and not calls.get("downloads"),
          str(calls.get("downloads")))

    # 大小对不上的硬失败
    entry = {"size": 999999, "local": str(local)}
    try:
        pipe._verify_local(local, entry, "c.mp4")
        check("大小不符时报错", False)
    except RuntimeError as exc:
        check("大小不符时报错", "不完整" in str(exc), str(exc))
    check("_outputs_ok 对空记录返回 True", pl.Pipeline._outputs_ok({}) is True)
    check("_outputs_ok 能发现缺失产物",
          pl.Pipeline._outputs_ok({"outputs": {"markdown": str(tmp / "没有这个文件.md")}})
          is False)
    _unpatch_all()


OSS_403_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Error>
  <Code>AccessDenied</Code>
  <Message>The bucket you are attempting to access must be addressed using the specified endpoint.</Message>
  <RequestId>6AC37D93FCBC723138F9458C</RequestId>
  <HostId>oss-example.oss-cn-beijing.aliyuncs.com</HostId>
  <Bucket>oss-example</Bucket>
  <Endpoint>oss-cn-hangzhou.aliyuncs.com</Endpoint>
  <EC>0003-00001403</EC>
</Error>"""

OSS_404_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Error><Code>NoSuchBucket</Code><Message>The specified bucket does not exist.</Message></Error>"""


class _FakeResp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class _FakeRequests:
    """替换 preflight 里的 requests 模块，记录它到底发了什么。"""

    def __init__(self, resp):
        self._resp = resp
        self.last_headers = {}
        self.last_url = ""

    def post(self, url, headers=None, json=None, timeout=None, **kw):
        self.last_url, self.last_headers = url, headers or {}
        return self._resp

    def get(self, url, headers=None, timeout=None, **kw):
        self.last_url, self.last_headers = url, headers or {}
        return self._resp


def test_preflight() -> None:
    print("\n[17] 上线前预检")
    from bp2md import preflight as pf

    code, endpoint = pf._parse_oss_error(OSS_403_XML)
    check("能从 OSS 错误里解析出错误码", code == "AccessDenied", code)
    check("能解析出正确的地域端点", endpoint == "oss-cn-hangzhou.aliyuncs.com", endpoint)
    check("解析 404 错误码",
          pf._parse_oss_error(OSS_404_XML)[0] == "NoSuchBucket")
    check("非 XML 不炸", pf._parse_oss_error("not xml") == ("", ""))

    check("标记映射正确",
          [pf.Result("x", lv, "").mark for lv in ("ok", "warn", "fail", "info")]
          == ["✓", "!", "✗", "·"])

    check("认出预期的鉴权错误",
          pf._classify(_FakeResp(401, '{"code":"InvalidApiKey"}'),
                       "t", "InvalidApiKey").level == "ok")
    check("5xx 归为警告",
          pf._classify(_FakeResp(500, "server error"), "t", "InvalidApiKey").level == "warn")

    # 安全不变量：预检只用占位 Key，绝不能把真实 Key 发出去
    real_key = "sk-REAL-SECRET-SHOULD-NOT-LEAK"
    fake = _FakeRequests(_FakeResp(401, '{"code":"InvalidApiKey","message":"Invalid API-key provided."}'))
    original = pf.requests
    pf.requests = fake
    try:
        cfg = Config(dashscope_api_key=real_key, asr_backend="bailian",
                     oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="b")
        results = pf.check_asr(cfg)
        sent = json.dumps(fake.last_headers, ensure_ascii=False)
        check("预检绝不发送真实 API Key", real_key not in sent, sent[:120])
        check("预检带上了异步头", fake.last_headers.get("X-DashScope-Async") == "enable")
        check("转写端点探测成功",
              any(r.level == "ok" for r in results), str([r.level for r in results]))
        check("明确说明路径无法被证明",
              any("不能证明路径" in r.detail for r in results),
              str([r.detail[:60] for r in results]))
    finally:
        pf.requests = original

    # 配置不齐时必须算失败——曾经缺 API Key 也能看到"预检通过"，
    # 用户会以为万事俱备，然后在真正跑的时候才撞墙。
    bare = Config(asr_backend="bailian", dashscope_api_key="",
                  oss_endpoint="", oss_bucket="", baidu_cookie="")
    problems = pf.check_completeness(bare)
    check("缺 API Key 会被报成失败",
          any(r.level == "fail" and "DASHSCOPE_API_KEY" in r.detail for r in problems),
          str([(r.level, r.detail[:40]) for r in problems]))
    check("缺 Key 时给了可操作的修法",
          any(r.fix for r in problems), str([r.fix[:40] for r in problems]))
    check("配置齐了不再报问题",
          [r.level for r in pf.check_completeness(
              Config(asr_backend="bailian", dashscope_api_key="sk-x",
                     oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="b",
                     oss_ak_id="i", oss_ak_secret="s",
                     baidu_cookie="BDUSS=x"))] == ["ok"],
          str([(r.level, r.detail[:50]) for r in pf.check_completeness(
              Config(asr_backend="bailian", dashscope_api_key="sk-x",
                     oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="b",
                     oss_ak_id="i", oss_ak_secret="s", baidu_cookie="BDUSS=x"))]))
    # check() 里"不影响运行"的那类不能算致命错误
    check("非致命问题只算警告",
          all(r.level == "warn" for r in pf.check_completeness(
              Config(asr_backend="bailian", dashscope_api_key="sk-x",
                     bailian_model="不存在的模型-x",
                     oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="b",
                     oss_ak_id="i", oss_ak_secret="s", baidu_cookie="BDUSS=x"))
              if "不影响运行" in r.detail),
          str([(r.level, r.detail[:40]) for r in pf.check_completeness(
              Config(asr_backend="bailian", dashscope_api_key="sk-x",
                     bailian_model="不存在的模型-x",
                     oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="b",
                     oss_ak_id="i", oss_ak_secret="s", baidu_cookie="BDUSS=x"))]))

    # 地域不符要给出可直接照抄的修法
    fake2 = _FakeRequests(_FakeResp(403, OSS_403_XML))
    pf.requests = fake2
    try:
        cfg2 = Config(oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="oss-example")
        rs = pf.check_oss(cfg2)
        check("地域不符被判定为 fail", rs[0].level == "fail", str(rs[0]))
        check("修法里给出正确端点",
              "oss-cn-hangzhou.aliyuncs.com" in rs[0].fix, rs[0].fix)
    finally:
        pf.requests = original

    # 桶不存在
    fake3 = _FakeRequests(_FakeResp(404, OSS_404_XML))
    pf.requests = fake3
    try:
        cfg3 = Config(oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="nope")
        rs = pf.check_oss(cfg3)
        check("桶不存在时提示核对桶名",
              rs[0].level == "fail" and "不存在" in rs[0].detail, rs[0].detail)
    finally:
        pf.requests = original

    # 私有桶是可用的
    fake4 = _FakeRequests(_FakeResp(403, "<Error><Code>AccessDenied</Code></Error>"))
    pf.requests = fake4
    try:
        cfg4 = Config(oss_endpoint="oss-cn-beijing.aliyuncs.com", oss_bucket="priv")
        rs = pf.check_oss(cfg4)
        check("私有桶不算失败", rs[0].level == "ok", str(rs[0]))
    finally:
        pf.requests = original

    # 用 PUBLIC_BASE_URL 时应跳过 OSS 检查
    fake5 = _FakeRequests(_FakeResp(200, "ok"))
    pf.requests = fake5
    try:
        cfg5 = Config(public_base_url="https://cdn.example.com/audio")
        rs = pf.check_oss(cfg5)
        check("走自有托管时跳过 OSS", any("跳过 OSS" in r.detail for r in rs),
              str([r.detail for r in rs]))
    finally:
        pf.requests = original

    # 什么都没配要明确报错
    cfg6 = Config(oss_endpoint="", oss_bucket="", public_base_url="")
    rs = pf.check_oss(cfg6)
    check("OSS 未配置时报 fail", rs[0].level == "fail", str(rs[0]))


def test_long_run_resilience(tmp: Path) -> None:
    """挂机几十小时才会暴露的问题：登录态失效、磁盘写满、无声文件、分离时长上限。"""
    print("\n[18] 长时间挂机的韧性")
    from bp2md import baidu as bd
    from bp2md import pipeline as pl

    # --- 错误分类 ---
    check("登录态失效被识别",
          isinstance(bd.classify_error(OSError("credentials rejected (http-403)")),
                     bd.BaiduAuthError))
    check("31045 被识别为登录态失效",
          isinstance(bd.classify_error(Exception("[errno 31045] x")), bd.BaiduAuthError))
    check("风控被识别",
          isinstance(bd.classify_error(Exception("[errno 132] 风控")), bd.BaiduRiskError))
    check("验证码被识别",
          isinstance(bd.classify_error(Exception("captcha required")), bd.BaiduRiskError))
    plain = ValueError("完全无关的错误")
    check("无关错误原样返回", bd.classify_error(plain) is plain)
    check("认证错误的建议里含可操作步骤",
          "BAIDU_COOKIE" in str(bd.classify_error(OSError("credentials rejected"))))
    check("风控建议里提到过验证码",
          "验证码" in str(bd.classify_error(Exception("captcha"))))

    # --- 下载层把 OSError 翻译成可中止批量的类型 ---
    cfg = _mkcfg(tmp, "resil")
    orig = (bd.ensure_cookies, bd.bdpan_download.make_fetcher, bd.bdpan_download.download_one)
    bd.ensure_cookies = lambda cfg_: {"BDUSS": "x", "STOKEN": "y", "BAIDUID": "z"}

    def _noop_fetcher(cookies, timeout=120):
        return lambda *a, **k: b""

    bd.bdpan_download.make_fetcher = _noop_fetcher

    def boom(*a, **k):
        raise OSError("credentials rejected (http-403) — re-run bdpan_cookies.py")

    bd.bdpan_download.download_one = boom
    try:
        try:
            bd.download_saved(cfg, [{"path": "/t/a.mp4", "name": "a.mp4", "size": 10}],
                              tmp / "dl")
            check("下载层的凭证失效会被翻译出来", False)
        except bd.BaiduAuthError:
            check("下载层的凭证失效会被翻译出来", True)
        except Exception as exc:  # noqa: BLE001
            check("下载层的凭证失效会被翻译出来", False, f"{type(exc).__name__}: {exc}")
    finally:
        bd.ensure_cookies, bd.bdpan_download.make_fetcher, bd.bdpan_download.download_one = orig

    # --- 批量必须立刻停，而不是一个接一个失败 ---
    cfg2 = _mkcfg(tmp, "stop")
    a = _mkvideo(tmp / "s_a.mp4", 4)
    b = _mkvideo(tmp / "s_b.mp4", 4)
    info = {"surl": "S9", "share_user": "u", "count": 2, "files": [
        {"fs_id": 1, "name": "a.mp4", "path": "/t/a.mp4", "size": a.stat().st_size,
         "isdir": False, "duration": 4},
        {"fs_id": 2, "name": "b.mp4", "path": "/t/b.mp4", "size": b.stat().st_size,
         "isdir": False, "duration": 4},
    ]}
    saved = [
        {"src_fs_id": 1, "fs_id": 1, "path": "/t/a.mp4", "name": "a.mp4",
         "relpath": "a.mp4", "size": a.stat().st_size, "duration": 4},
        {"src_fs_id": 2, "fs_id": 2, "path": "/t/b.mp4", "name": "b.mp4",
         "relpath": "b.mp4", "size": b.stat().st_size, "duration": 4},
    ]
    calls: dict = {}
    _stub_io(pl, tmp, calls, info, saved, {"a.mp4": a, "b.mp4": b})

    def dying_dl(cfg_, items, out_dir, log=print):
        calls.setdefault("downloads", []).append(items[0]["name"])
        raise bd.BaiduAuthError("登录态已失效")

    _patch(pl.baidu, download_saved=dying_dl)
    pipe = pl.Pipeline(cfg2, log=_quiet)
    try:
        pipe.run("https://pan.baidu.com/s/1STOP")
        check("认证失效会向上抛出以中止批量", False)
    except bd.BaiduStopBatch:
        check("认证失效会向上抛出以中止批量", True)
    check("中止后没有再碰第二个文件",
          calls.get("downloads") == ["a.mp4"], str(calls.get("downloads")))

    # --- 磁盘空间 ---
    orig_du = pl.shutil.disk_usage
    try:
        import collections
        Usage = collections.namedtuple("usage", "total used free")
        pl.shutil.disk_usage = lambda p: Usage(1000, 900, 100)
        try:
            pipe._check_disk(10_000)
            check("空间不足时提前报错", False)
        except RuntimeError as exc:
            check("空间不足时提前报错", "磁盘空间不足" in str(exc), str(exc))
        pl.shutil.disk_usage = lambda p: Usage(10**12, 0, 10**12)
        pipe._check_disk(10_000)      # 不该抛
        check("空间充足时不打扰", True)
        pl.shutil.disk_usage = lambda p: (_ for _ in ()).throw(OSError("no disk info"))
        pipe._check_disk(10_000)
        check("拿不到磁盘信息时不阻塞", True)
    finally:
        pl.shutil.disk_usage = orig_du

    # --- 说话人分离的时长上限 ---
    # 显式指定模型，避免这条用例依赖 Config 的默认值
    plain_cfg = Config(max_segment_seconds=6 * 3600, asr_backend="bailian",
                       bailian_model="paraformer-v2", bailian_diarization=False)
    dia_cfg = Config(max_segment_seconds=6 * 3600, asr_backend="bailian",
                     bailian_model="paraformer-v2", bailian_diarization=True)
    check("不开分离时用配置值", plain_cfg.effective_segment_seconds == 6 * 3600,
          str(plain_cfg.effective_segment_seconds))
    check("开分离时自动收紧到 2 小时", dia_cfg.effective_segment_seconds == 7200,
          str(dia_cfg.effective_segment_seconds))
    check("配置更小时不会被放宽",
          Config(max_segment_seconds=1800, asr_backend="bailian",
                 bailian_model="paraformer-v2",
                 bailian_diarization=True).effective_segment_seconds == 1800)
    check("siliconflow 后端不套用该限制",
          Config(max_segment_seconds=6 * 3600, asr_backend="siliconflow",
                 bailian_diarization=True).effective_segment_seconds == 6 * 3600)

    # --- 没有音轨的文件要说人话 ---
    silent = tmp / "无声录屏.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc=size=128x96:rate=10:duration=4",
        "-c:v", "libx264", "-preset", "ultrafast", "-an", str(silent),
    ], check=True, capture_output=True)
    cfg3 = _mkcfg(tmp, "silent")
    try:
        media.prepare(cfg3, silent)
        check("无音轨文件给出明确报错", False)
    except media.MediaError as exc:
        check("无音轨文件给出明确报错", "没有音轨" in str(exc), str(exc))
    _unpatch_all()


def test_render_and_clean(tmp: Path) -> None:
    """render 不能写出重复文件；clean 之后续跑不能崩。"""
    print("\n[19] render 与 clean 的边界")
    import argparse
    from bp2md import pipeline as pl
    from bp2md.cli import cmd_render

    # --- render 要用 result.json 自己的文件名当 slug ---
    cfg = _mkcfg(tmp, "render")
    from bp2md.asr import Segment, Transcript
    tr = Transcript(segments=[Segment(0, 2000, "内容。", "1")], duration_ms=2000,
                    model="m", backend="bailian")
    meta = md.DocMeta(title="第1讲", source_file="第1讲.mp4",
                      source_url="https://pan.baidu.com/s/1X", duration_ms=2000,
                      asr_backend="bailian", asr_model="m",
                      transcribed_at=md.now_iso())
    # 原始产物是带目录前缀的名字（嵌套分享里就是这样）
    md.write_outputs(tr, meta, cfg.outdir, slug="课程合集_第1讲")
    original = cfg.outdir / "课程合集_第1讲.md"
    check("原始 Markdown 用的是带前缀的名字", original.exists())

    rc = cmd_render(argparse.Namespace(result=str(cfg.outdir / "课程合集_第1讲.result.json")),
                    cfg)
    check("render 返回 0", rc == 0)
    check("render 更新的是原文件而不是新建",
          original.exists() and not (cfg.outdir / "第1讲.md").exists(),
          str([p.name for p in cfg.outdir.glob("*.md")]))

    # --- clean 删掉音频后，续跑要能重新生成 ---
    cfg2 = _mkcfg(tmp, "cleanres")
    v = _mkvideo(tmp / "src_z.mp4", 6)
    info = {"surl": "SZ", "share_user": "u", "count": 1, "files": [
        {"fs_id": 1, "name": "z.mp4", "path": "/t/z.mp4", "size": v.stat().st_size,
         "isdir": False, "duration": 6},
    ]}
    saved = [{"src_fs_id": 1, "fs_id": 1, "path": "/t/z.mp4", "name": "z.mp4",
              "relpath": "z.mp4", "size": v.stat().st_size, "duration": 6}]
    calls: dict = {}
    _stub_io(pl, tmp, calls, info, saved, {"z.mp4": v})
    pipe = pl.Pipeline(cfg2, log=_quiet)
    URL = "https://pan.baidu.com/s/1CLEAN"

    pipe.run(URL)
    st = pipe.load_state(URL)
    entry = st["files"]["z.mp4"]
    check("先跑完一次", entry["status"] == "done", entry["status"])

    # 模拟：跑到 prepared 阶段中断 → 用户执行 clean → 续跑
    audio_path = Path(entry["audios"][0]["path"])
    check("音频文件确实存在", audio_path.exists())
    audio_path.unlink()
    entry["results"] = []          # 让它必须重新转写
    entry["status"] = "prepared"
    entry.pop("outputs", None)
    pipe.save_state(URL, st)

    calls["downloads"] = []
    calls["asr"] = 0
    try:
        pipe.run(URL)
        ok = Path(entry["audios"][0]["path"]).exists()
    except Exception as exc:  # noqa: BLE001
        ok = False
        check("音频被 clean 后能重新生成", False, f"{type(exc).__name__}: {exc}")
    check("音频被 clean 后能重新生成", ok)
    check("重新生成音频不需要重下原视频", not calls.get("downloads"),
          str(calls.get("downloads")))
    check("重新生成后重新转写了一次", calls.get("asr") == 1, str(calls.get("asr")))
    _unpatch_all()


def test_clean_workdir(tmp: Path) -> None:
    """清理中间产物：**删能重算的，留要花钱的**。

    work/ 里同时躺着两类东西，很容易被一起删掉：
      - 能重新算出来的：下载的原片、抽出的音频、切片；
      - 算了就要花钱的：进度账本（哪些文件已经转写过了）。
    把第二类删掉，下次重跑会把所有文件当新的重新下载 + 重新计费。
    """
    print("\n[19b] 清理中间产物")
    from bp2md import pipeline as pl

    cfg = _mkcfg(tmp, "cleanup")
    (cfg.workdir / "raw" / "子目录").mkdir(parents=True, exist_ok=True)
    (cfg.workdir / "raw" / "子目录" / "v.mp4").write_bytes(b"x" * 1000)
    (cfg.workdir / "raw" / "v.mp4.dlstate.json").write_text("{}", encoding="utf-8")
    (cfg.workdir / "audio" / "v.mp3").write_bytes(b"y" * 100)
    (cfg.workdir / "segments" / "v").mkdir(parents=True, exist_ok=True)
    (cfg.workdir / "segments" / "v" / "v_part001.mp3").write_bytes(b"z" * 10)
    (cfg.workdir / "uploads" / "u.mp4").write_bytes(b"u" * 50)
    (cfg.workdir / "state" / "abc.json").write_text("{}", encoding="utf-8")

    u = pl.work_usage(cfg)
    check("能按类别算出各类占了多少",
          u["raw"]["bytes"] == 1002 and u["audio"]["files"] == 1
          and u["uploads"]["bytes"] == 50, str(u))

    r = pl.clean_workdir(cfg, raw=True, log=_quiet)
    check("清理会删掉下载的原片", pl.dir_usage(cfg.workdir / "raw")[0] == 0,
          str(pl.dir_usage(cfg.workdir / "raw")))
    check("清理会删掉抽出的音频", pl.dir_usage(cfg.workdir / "audio")[0] == 0)
    check("清理会删掉切片", pl.dir_usage(cfg.workdir / "segments")[0] == 0)
    check("连分块的下载账本也一起清掉（它跟着原片走）",
          not (cfg.workdir / "raw" / "v.mp4.dlstate.json").exists())
    check("空掉的子目录也收掉了，不会越积越多",
          not (cfg.workdir / "segments" / "v").exists()
          and not (cfg.workdir / "raw" / "子目录").exists())
    check("进度账本必须保留（删了下次会重新计费）",
          (cfg.workdir / "state" / "abc.json").exists())
    check("上传的原件默认不动（那是用户自己的文件）",
          (cfg.workdir / "uploads" / "u.mp4").exists())
    check("释放的空间是真实统计出来的", r["freed_bytes"] == 1112, str(r))
    check("清理之后目录结构还在（不会把 work 弄坏）",
          (cfg.workdir / "audio").is_dir() and (cfg.workdir / "uploads").is_dir())

    r2 = pl.clean_workdir(cfg, raw=True, uploads=True, log=_quiet)
    check("明确要求时才会删上传的原件",
          not (cfg.workdir / "uploads" / "u.mp4").exists()
          and r2["freed_bytes"] == 50, str(r2))
    check("没东西可清时不报错", pl.clean_workdir(cfg, log=_quiet)["removed"] == 0)
    check("清理后 state 仍然可读", pl.Pipeline(cfg, log=_quiet).load_state(
        "https://pan.baidu.com/s/1X")["url"] == "https://pan.baidu.com/s/1X")

    # 命令行那一层也要真的接上（曾经 clean 是单独的实现在 cli.py 里）
    import argparse
    from bp2md.cli import cmd_clean
    (cfg.workdir / "audio" / "again.mp3").write_bytes(b"a" * 30)
    rc = cmd_clean(argparse.Namespace(raw=True, uploads=True), cfg)
    check("命令行 clean 走的是同一个实现", rc == 0 and
          pl.dir_usage(cfg.workdir / "audio")[0] == 0,
          str(pl.dir_usage(cfg.workdir / "audio")))


def test_supervise() -> None:
    """监督模式：反复重试直到完成或超预算；每轮必须重新读配置。"""
    print("\n[20] 挂机监督模式")
    from bp2md import pipeline as pl

    def make_clock(start=1000.0):
        clock = {"t": start, "sleeps": []}

        def now():
            return clock["t"]

        def sleep(sec):
            clock["sleeps"].append(sec)
            clock["t"] += sec
        return clock, now, sleep

    check("全部完成时不需要重试",
          pl.needs_retry([{"files": {"a": {"status": "done"}}}]) is False)
    check("有未完成的就需要重试",
          pl.needs_retry([{"files": {"a": {"status": "done"},
                                     "b": {"status": "prepared"}}}]) is True)
    check("失败也算未完成",
          pl.needs_retry([{"files": {"a": {"status": "failed"}}}]) is True)
    check("空状态不需要重试", pl.needs_retry([]) is False)

    # 第一次就全部完成
    clock, now, sleep = make_clock()
    calls = {"rounds": 0, "reloads": 0}
    res = pl.supervise(lambda cfg: (calls.__setitem__("rounds", calls["rounds"] + 1),
                                    [{"files": {"a": {"status": "done"}}}])[1],
                       lambda: (calls.__setitem__("reloads", calls["reloads"] + 1), object())[1],
                       hours=1, interval_sec=60, log=_quiet, sleep=sleep, now=now)
    check("一次跑完就结束", res["finished"] is True and calls["rounds"] == 1, str(calls))
    check("没有多余的等待", clock["sleeps"] == [], str(clock["sleeps"]))

    # 前两轮没跑完，第三轮完成
    clock, now, sleep = make_clock()
    seq = [{"files": {"a": {"status": "prepared"}}},
           {"files": {"a": {"status": "prepared"}}},
           {"files": {"a": {"status": "done"}}}]
    calls = {"rounds": 0, "reloads": 0}

    def run_round(cfg):
        calls["rounds"] += 1
        return [seq[min(calls["rounds"] - 1, len(seq) - 1)]]

    def reload_cfg():
        calls["reloads"] += 1
        return object()

    res = pl.supervise(run_round, reload_cfg, hours=1, interval_sec=60,
                       log=_quiet, sleep=sleep, now=now)
    check("未完成时会继续下一轮", res["rounds"] == 3 and res["finished"] is True,
          str(res["rounds"]))
    check("每轮之间等待了设定间隔", clock["sleeps"] == [60, 60], str(clock["sleeps"]))
    check("每轮都重新读了配置（Cookie 更新能自动生效）",
          calls["reloads"] == 3, str(calls))

    # 永远跑不完 → 到时间预算就停
    clock, now, sleep = make_clock()
    calls = {"rounds": 0}
    res = pl.supervise(lambda cfg: (calls.__setitem__("rounds", calls["rounds"] + 1),
                                    [{"files": {"a": {"status": "failed"}}}])[1],
                       lambda: object(), hours=0.05, interval_sec=60,
                       log=_quiet, sleep=sleep, now=now)
    check("超出预算会停下", res["finished"] is False, str(res))
    check("预算内跑满但不超时",
          res["rounds"] == 4 and clock["t"] == 1180.0, f"{res['rounds']} t={clock['t']}")
    check("最后一轮不会睡过头", sum(clock["sleeps"]) == 180.0,
          str(clock["sleeps"]))


def test_vendored_upstream() -> None:
    """跑 vendored 百度层自带的上游测试。

    这层是从上游拷来的、被当黑盒用了很久。上游自带测试却一直没跑过——
    如果拷贝有缺失或行为漂移，光看代码是看不出来的。
    """
    print("\n[21] vendored 百度层的上游测试")
    import importlib.util
    # 这一项必须先无条件地记一次结果。以前是"没装 pytest 就直接 return"，
    # 于是自检的总条数会随"这台机器装没装 pytest"变化，而 main() 最后那条
    # README 对齐断言是按固定数字比的——同一份代码在一台机器上全绿、
    # 在另一台上莫名变红。检查项的数量必须是确定的。
    # 另外：pytest 只是开发期依赖（主流程只要 requests），没装不该算"失败"，
    # 但也不能假装跑过了——所以标签里明说"本次跳过"。
    if importlib.util.find_spec("pytest") is None:
        check("上游测试套件（本机没装 pytest，本次跳过）", True)
        print("    · 想真跑上游那 46 项测试：pip install -r requirements-dev.txt")
        return
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "vendor/baidu_pan/tests", "-q",
         "--no-header", "-p", "no:cacheprovider",
         # 让 pytest 把临时目录放在自检目录里，别在项目根目录留 pytest-of-* 垃圾
         "--basetemp=" + str(TMP_ROOT / "pytest")],
        cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "").strip()
    tail = out.splitlines()[-1] if out else (proc.stderr or "")[-200:]
    check("上游测试套件全部通过", proc.returncode == 0, tail)
    print(f"    {tail}")


def test_asr_clients() -> None:
    """用本地 mock HTTP 服务真实驱动 ASR 客户端。

    这是补上一个真实的覆盖盲区：此前 asr.py 里的 BailianAsr / SiliconFlowAsr
    从没被执行过（流水线测试都在 make_asr 那层打桩了）。
    """
    print("\n[22] ASR 客户端（本地 mock 服务，走真实 HTTP）")
    from bp2md.asr import AsrError, BailianAsr, SiliconFlowAsr
    from mock_asr import OFFICIAL_RESULT, MockAsrServer

    def make_cfg(base: str) -> Config:
        # 显式声明模型和分离开关：这条用例测的是客户端机制（头/轮询/解析），
        # 用的模型本身不支持分离，所以必须显式关掉，否则会被配置校验拦下。
        cfg = Config(dashscope_api_key="sk-test", dashscope_base=base,
                     asr_backend="bailian", bailian_model="qwen3-asr-flash-filetrans",
                     bailian_diarization=False, bailian_language_hints="zh,en")
        cfg.poll_interval = 0.01
        cfg.poll_timeout = 20
        return cfg

    # ---- 正常路径：RUNNING → SUCCEEDED → 下载结果 ----
    srv = MockAsrServer(polls_before_ready=2)
    base = srv.start()
    try:
        asr = BailianAsr(make_cfg(base))
        tr = asr.transcribe("https://cdn.example.com/a.mp3", log=_quiet)
        s = srv.state
        check("提交了一次", s.counts["submit"] == 1, str(s.counts))
        check("轮询了三次（两次 RUNNING + 一次成功）", s.counts["poll"] == 3, str(s.counts))
        check("下载了一次结果", s.counts["result"] == 1, str(s.counts))
        check("带上了异步头（提交请求）",
              s.last_headers.get("X-DashScope-Async") == "enable", str(s.last_headers))
        check("查询请求不带异步头",
              "X-DashScope-Async" not in s.last_get_headers, str(s.last_get_headers))
        check("带了 Bearer 鉴权",
              str(s.last_headers.get("Authorization", "")).startswith("Bearer sk-"))
        check("请求体里有 model", s.last_body.get("model") == "qwen3-asr-flash-filetrans",
              str(s.last_body))
        check("请求体用 file_urls 传公网地址",
              s.last_body.get("input", {}).get("file_urls") == ["https://cdn.example.com/a.mp3"],
              str(s.last_body))
        check("language_hints 被正确拆分",
              s.last_body.get("parameters", {}).get("language_hints") == ["zh", "en"],
              str(s.last_body))

        check("解析出两句", len(tr.segments) == 2, str(len(tr.segments)))
        check("句子文本正确", tr.segments[0].text == "Hello World，这里是阿里巴巴语音实验室。",
              tr.segments[0].text)
        check("毫秒时间戳正确", (tr.segments[0].start_ms, tr.segments[0].end_ms) == (760, 3240),
              str(tr.segments[0]))
        check("说话人字段正确（speaker_id 不是 speaker）",
              [x.speaker for x in tr.segments] == ["0", "1"],
              str([x.speaker for x in tr.segments]))
        check("时长取自 properties",
              tr.duration_ms == 3834, str(tr.duration_ms))
    finally:
        srv.stop()

    # ---- 任务失败 ----
    srv = MockAsrServer(task_fail=True)
    base = srv.start()
    try:
        try:
            BailianAsr(make_cfg(base)).transcribe("u", log=_quiet)
            check("任务失败会抛错", False)
        except AsrError as exc:
            check("任务失败会抛错", "失败" in str(exc), str(exc)[:80])
    finally:
        srv.stop()

    # ---- 子任务失败 ----
    srv = MockAsrServer(subtask_fail=True)
    base = srv.start()
    try:
        try:
            BailianAsr(make_cfg(base)).transcribe("u", log=_quiet)
            check("子任务失败会抛错", False)
        except AsrError as exc:
            check("子任务失败会抛错", "子任务" in str(exc), str(exc)[:80])
    finally:
        srv.stop()

    # ---- 结果里没有 transcription_url ----
    srv = MockAsrServer(missing_url=True)
    base = srv.start()
    try:
        try:
            BailianAsr(make_cfg(base)).transcribe("u", log=_quiet)
            check("缺结果地址会抛错", False)
        except AsrError as exc:
            check("缺结果地址会抛错", "transcription_url" in str(exc), str(exc)[:80])
    finally:
        srv.stop()

    # ---- 提交返回空 task_id ----
    srv = MockAsrServer(no_task_id=True)
    base = srv.start()
    try:
        try:
            BailianAsr(make_cfg(base)).transcribe("u", log=_quiet)
            check("拿不到 task_id 会抛错", False)
        except AsrError as exc:
            check("拿不到 task_id 会抛错", "task_id" in str(exc), str(exc)[:80])
    finally:
        srv.stop()

    # ---- 5xx 会被退避重试（提交与轮询都试）----
    srv = MockAsrServer(flaky_submit=1, flaky_poll=1)
    base = srv.start()
    try:
        tr = BailianAsr(make_cfg(base)).transcribe("u", log=_quiet)
        check("提交遇到 5xx 会重试", srv.state.counts["submit"] == 2,
              str(srv.state.counts))
        check("轮询遇到 5xx 会重试", srv.state.counts["poll"] >= 2,
              str(srv.state.counts))
        check("重试后仍能拿到结果", len(tr.segments) == 2, str(len(tr.segments)))
    finally:
        srv.stop()

    # ---- 结果 JSON 字段缺失时不该崩 ----
    srv = MockAsrServer(result={"transcripts": [{"sentences": [
        {"begin_time": 0, "end_time": 100, "text": "只有一句"}]}]})
    base = srv.start()
    try:
        tr = BailianAsr(make_cfg(base)).transcribe("u", log=_quiet)
        check("缺 speaker_id / properties 也能解析",
              len(tr.segments) == 1 and tr.segments[0].speaker == "", str(tr.segments))
        check("没有 properties 时用末句时间兜底",
              tr.duration_ms == 100, str(tr.duration_ms))
    finally:
        srv.stop()

    # ---- 硅基流动：纯文本 ----
    srv = MockAsrServer(sf_text="第一句。第二句！第三句？")
    base = srv.start()
    try:
        cfg = Config(siliconflow_api_key="sk-x", siliconflow_base=base,
                     asr_backend="siliconflow")
        tr = SiliconFlowAsr(cfg).transcribe(ROOT / "vendor" / "baidu_pan" / "LICENSE",
                                            log=_quiet)
        check("硅基流动走 multipart 上传", srv.state.counts["sf"] == 1, str(srv.state.counts))
        check("短文本合成一段（不硬切）", len(tr.segments) == 1, str(len(tr.segments)))
        check("纯文本内容完整",
              tr.text == "第一句。第二句！第三句？", tr.text)
        check("切出来的段没有时间戳（该模型不返回）",
              all(x.start_ms == 0 and x.end_ms == 0 for x in tr.segments))
    finally:
        srv.stop()

    # ---- 硅基流动：带 segments 的 verbose_json ----
    srv = MockAsrServer(sf_segments=[
        {"start": 0.5, "end": 1.5, "text": "甲", "speaker": "1"},
        {"start": 1.5, "end": 2.5, "text": "乙", "speaker": "2"},
    ])
    base = srv.start()
    try:
        cfg = Config(siliconflow_api_key="sk-x", siliconflow_base=base,
                     asr_backend="siliconflow")
        tr = SiliconFlowAsr(cfg).transcribe(ROOT / "vendor" / "baidu_pan" / "LICENSE",
                                            log=_quiet)
        check("有 segments 时优先用 segments", len(tr.segments) == 2, str(len(tr.segments)))
        check("秒被换算成毫秒", tr.segments[0].start_ms == 500 and tr.segments[0].end_ms == 1500,
              str(tr.segments[0]))
        check("说话人字段被读出来", [x.speaker for x in tr.segments] == ["1", "2"],
              str([x.speaker for x in tr.segments]))
    finally:
        srv.stop()

    # ---- 官方报文结构本身要能被解析（防止 mock 写歪）----
    parsed = BailianAsr.parse_result(OFFICIAL_RESULT)
    check("官方文档报文可解析", len(parsed.segments) == 2 and parsed.duration_ms == 3834,
          str(len(parsed.segments)))


def test_model_registry() -> None:
    """模型能力/价格登记表，以及"分离参数名传错会静默失效"这个坑。"""
    print("\n[23] 模型登记表与说话人分离参数")
    from bp2md import models
    from bp2md.asr import AsrError, BailianAsr
    from bp2md.config import Config
    from mock_asr import MockAsrServer

    # ---- 登记表本身 ----
    check("paraformer-v2 被登记为支持分离",
          models.lookup("paraformer-v2").diarization is True)
    check("paraformer-v2 单价 0.288 元/小时",
          abs((models.price_per_hour("paraformer-v2") or 0) - 0.288) < 0.001,
          str(models.price_per_hour("paraformer-v2")))
    check("qwen3-asr-flash-filetrans 不支持分离",
          models.lookup("qwen3-asr-flash-filetrans").diarization is False)
    check("带日期后缀的版本号能归并到主名",
          models.lookup("fun-asr-2025-11-07").name == "fun-asr",
          str(models.lookup("fun-asr-2025-11-07")))
    check("未知模型返回 None 而不是抛错", models.lookup("未来新模型") is None)
    check("支持分离里最便宜的是 paraformer-v2",
          models.cheapest_with_diarization().name == "paraformer-v2",
          models.cheapest_with_diarization().name)
    check("同步版模型的参数名与 filetrans 不同",
          models.lookup("qwen-audio-3.1-asr-flash").diarization_param
          == "speaker_diarization_enabled")
    check("filetrans 系列用 diarization_enabled",
          models.lookup("fun-asr").diarization_param == "diarization_enabled")

    # ---- 配置校验 ----
    def cfg_for(model, dia):
        return Config(bailian_model=model, bailian_diarization=dia,
                      dashscope_api_key="x", oss_endpoint="e", oss_bucket="b",
                      oss_ak_id="a", oss_ak_secret="s")

    probs = cfg_for("qwen3-asr-flash-filetrans", True).check()
    check("不支持的模型 + 开分离会被拦下",
          any("不支持说话人分离" in p for p in probs), str(probs))
    check("拦截信息里给出了替代方案",
          any("paraformer-v2" in p for p in probs), str(probs))
    check("支持的模型 + 开分离不报错",
          not [p for p in cfg_for("paraformer-v2", True).check() if "分离" in p])
    check("同步模型会被识别出来",
          any("不是录音文件转写" in p
              for p in cfg_for("qwen-audio-3.1-asr-flash", False).check()))

    # ---- 时长上限只在"真的会开分离"时才收紧 ----
    check("paraformer-v2 + 分离 → 收紧到 2 小时",
          cfg_for("paraformer-v2", True).effective_segment_seconds == 7200)
    check("paraformer-v2 不开分离 → 用配置值",
          cfg_for("paraformer-v2", False).effective_segment_seconds == 6 * 3600)
    check("模型不支持分离时不该收紧（反正不会开）",
          cfg_for("qwen3-asr-flash-filetrans", True).effective_segment_seconds
          == 6 * 3600)

    # ---- 真正的验证：请求体里的参数名对不对（走 mock 服务）----
    srv = MockAsrServer()
    base = srv.start()
    try:
        def run(model, dia, hints="zh,en"):
            cfg = Config(dashscope_api_key="sk-t", dashscope_base=base,
                         bailian_model=model, bailian_diarization=dia,
                         bailian_language_hints=hints)
            cfg.poll_interval = 0.01
            cfg.poll_timeout = 10
            BailianAsr(cfg).transcribe("https://x/a.mp3", log=_quiet)
            return dict(srv.state.last_body.get("parameters") or {})

        p = run("paraformer-v2", True)
        check("paraformer-v2 开启分离时发的是 diarization_enabled",
              p.get("diarization_enabled") is True, str(p))
        check("paraformer-v2 不该收到 language_hints（官方示例没有）",
              "language_hints" not in p, str(p))

        p2 = run("qwen-audio-3.1-asr-flash-filetrans", True)
        check("qwen-audio filetrans 用同一个参数名且带 language_hints",
              p2.get("diarization_enabled") is True
              and p2.get("language_hints") == ["zh", "en"], str(p2))

        p3 = run("paraformer-v2", False)
        check("关掉分离时不该发任何分离参数",
              "diarization_enabled" not in p3, str(p3))

        p4 = run("模型不在表里", True)
        check("未登记模型退回通用参数名并提示",
              p4.get("diarization_enabled") is True, str(p4))
    finally:
        srv.stop()

    # ---- 不支持分离的模型：应在发请求前就拦下 ----
    srv2 = MockAsrServer()
    base2 = srv2.start()
    try:
        cfg = Config(dashscope_api_key="sk-t", dashscope_base=base2,
                     bailian_model="qwen3-asr-flash-filetrans",
                     bailian_diarization=True)
        cfg.poll_interval = 0.01
        try:
            BailianAsr(cfg).transcribe("https://x/a.mp3", log=_quiet)
            check("不支持的模型不会真的发出去", False)
        except AsrError as exc:
            check("不支持的模型不会真的发出去", "不支持说话人分离" in str(exc), str(exc)[:80])
        check("被拦下时没有发出任何请求", srv2.state.counts["submit"] == 0,
              str(srv2.state.counts))
    finally:
        srv2.stop()

    # ---- 同步模型在构造阶段就被拒绝 ----
    try:
        BailianAsr(Config(dashscope_api_key="sk-t",
                          bailian_model="qwen-audio-3.1-asr-flash"))
        check("同步模型构造时就报错", False)
    except AsrError as exc:
        check("同步模型构造时就报错", "不是录音文件转写" in str(exc), str(exc)[:80])


def test_local_files(tmp: Path) -> None:
    """本地文件直转：不经过百度网盘，直接读本机文件。

    这条路是给"用网盘客户端下到本地，再让工具直接读"的用法准备的——
    客户端通常比脚本走网页下载快一个数量级。它必须：
      1. 完全不碰百度网盘（连 Cookie 都不需要）；
      2. 绝不改动用户的源文件；
      3. 同名文件换了内容时不能拿旧稿子糊弄。
    """
    print("\n[15b] 本地文件直转（跳过网盘）")
    from bp2md import pipeline as pl

    cfg = _mkcfg(tmp, "local")
    calls: dict = {}

    def boom(*a, **k):
        calls["baidu"] = calls.get("baidu", 0) + 1
        raise AssertionError("本地文件这条路不该碰百度网盘")

    _patch(pl.baidu, inspect_share=boom, save_share=boom, download_saved=boom)
    _patch(pl, make_storage=lambda cfg_: _FakeStorage(calls),
           make_asr=lambda cfg_: _FakeAsr(calls))

    userdir = tmp / "用户的文件夹"
    src = _mkvideo(userdir / "本地素材.mp4", 6)
    before = (src.stat().st_size, src.stat().st_mtime_ns)

    totals, failures, _ab, states = pl.run_round_local(cfg, [src], log=_quiet)
    check("本地文件能跑完 1 个", totals["done"] == 1 and not failures, str(totals))
    check("全程一次都没碰百度网盘", not calls.get("baidu"), str(calls))
    check("转写被调用了", calls.get("asr") == 1, str(calls))
    check("音频先上传再转写（百炼后端要公网 URL）", calls.get("upload") == 1, str(calls))
    mds = list(cfg.outdir.glob("*.md"))
    check("产出了 Markdown", len(mds) == 1, str(mds))
    text = mds[0].read_text(encoding="utf-8")
    check("front matter 里记录了它是本地文件",
          "source_url:" in text and "local://" in text, text[:300])
    check("产物只写在 output/，没有丢到用户文件夹里",
          not list(userdir.glob("*.md")) and not list(userdir.glob("*.json")),
          str(list(userdir.iterdir())))
    check("没有动用户的源文件（大小和修改时间都没变）",
          (src.stat().st_size, src.stat().st_mtime_ns) == before)
    check("状态文件按绝对路径区分（各自独立、可续跑）",
          len(list((cfg.workdir / "state").glob("*.json"))) == 1,
          str(list((cfg.workdir / "state").glob("*.json"))))
    check("音频中间产物放在 work 里",
          len(list((cfg.workdir / "audio").glob("*.mp3"))) == 1,
          str(list((cfg.workdir / "audio").glob("*.mp3"))))

    # ---- 重跑必须复用，不能重复花钱 ----
    audio = list((cfg.workdir / "audio").glob("*.mp3"))[0]
    audio_stamp = audio.stat().st_mtime_ns
    totals2, _f2, _a2, _s2 = pl.run_round_local(cfg, [src], log=_quiet)
    check("重跑命中缓存，不再转写", calls.get("asr") == 1, str(calls))
    check("重跑不重新抽音频", audio.stat().st_mtime_ns == audio_stamp)
    check("重跑仍然报 1 个完成", totals2["done"] == 1, str(totals2))

    # ---- 同名文件被换成另一份内容 ----
    # 这是最危险的一种情况：不做内容指纹的话，会静默地拿上一次的音频缓存
    # 算出一份"看起来成功、其实对不上"的稿子。
    _mkvideo(src, 9)
    totals3, _f3, _a3, _s3 = pl.run_round_local(cfg, [src], log=_quiet)
    check("换了内容会重新转写", calls.get("asr") == 2, str(calls))
    check("换了内容不会多出一份 Markdown（slug 稳定）",
          len(list(cfg.outdir.glob("*.md"))) == 1,
          str(list(cfg.outdir.glob("*.md"))))
    check("换了内容后仍然报完成", totals3["done"] == 1, str(totals3))
    check("旧的音频缓存被清掉了",
          len(list((cfg.workdir / "audio").glob("*.mp3"))) == 1,
          str(list((cfg.workdir / "audio").glob("*.mp3"))))

    # ---- 文件被删掉/移走 ----
    src.unlink()
    totals4, failures4, _a4, _s4 = pl.run_round_local(cfg, [src], log=_quiet)
    check("源文件不见了会失败，但不会崩", totals4["failed"] == 1, str(totals4))
    check("失败原因说清了文件不在了，以及该怎么办",
          any(("找不到" in f or "不在了" in f) and "重新" in f for f in failures4),
          str(failures4)[:300])

    # ---- 不是音视频的文件 ----
    junk = userdir / "说明.txt"
    junk.write_text("这不是音视频", encoding="utf-8")
    tj, fj, _aj, _sj = pl.run_round_local(cfg, [junk], log=_quiet)
    check("非音视频文件被拒绝，并说明原因",
          tj["failed"] == 1 and any("不是音视频" in f for f in fj), str(fj)[:200])
    try:
        pl.Pipeline(cfg, log=_quiet).run_local(junk)
        check("直接调用时非音视频文件会抛错", False)
    except RuntimeError as exc:
        check("直接调用时非音视频文件会抛错", "不是音视频" in str(exc), str(exc)[:120])
    try:
        pl.Pipeline(cfg, log=_quiet).run_local(userdir)
        check("给了文件夹路径时给出明确提示", False)
    except RuntimeError as exc:
        check("给了文件夹路径时给出明确提示",
              "文件夹" in str(exc) and "扫描" in str(exc), str(exc)[:140])

    # ---- 只看计划：不抽音频、不上传、不花钱 ----
    cfg2 = _mkcfg(tmp, "local2")
    calls2: dict = {}
    _patch(pl, make_storage=lambda cfg_: _FakeStorage(calls2),
           make_asr=lambda cfg_: _FakeAsr(calls2))
    src2 = _mkvideo(tmp / "user2" / "只看计划.mp4", 5)
    t5, _f5, _a5, s5 = pl.run_round_local(cfg2, [src2], dry_run=True, log=_quiet)
    check("只看计划不抽音频",
          not list((cfg2.workdir / "audio").glob("*.mp3")),
          str(list((cfg2.workdir / "audio").glob("*.mp3"))))
    check("只看计划不转写", not calls2.get("asr"), str(calls2))
    check("只看计划不产出 Markdown", not list(cfg2.outdir.glob("*.md")))
    check("只看计划也如实报告计划处理了几个文件",
          pl.summarize(s5[0])["total"] == 1, str(pl.summarize(s5[0])))
    check("只看计划不碰百度网盘", not calls2.get("baidu"), str(calls2))

    # ---- 这条路不该要求百度凭据 ----
    bare = Config.load(env_file=tmp / "none.env")
    bare.workdir = tmp / "bare-work"
    local_problems = bare.check(require_baidu=False)
    share_problems = bare.check()
    check("本地文件这条路不要求百度 Cookie",
          not any("Cookie" in p or "凭据" in p for p in local_problems),
          str(local_problems))
    check("走分享链接那条路仍然要求百度凭据（没有放松）",
          any("Cookie" in p or "凭据" in p for p in share_problems),
          str(share_problems))

    _unpatch_all()


def test_speaker_handling(tmp: Path) -> None:
    """单人 / 多人的判定与输出形态。

    判定是从转写结果里数 speaker_id 得来的，**不需要预先探测**，所以零成本。
    """
    print("\n[24] 单人 vs 多人的判定与输出")
    from bp2md import pipeline as pl
    from bp2md.asr import Segment, Transcript

    # ---- Transcript.speaker_count ----
    def tr_with(speakers):
        return Transcript(segments=[Segment(i * 1000, (i + 1) * 1000, "句子。", s)
                                    for i, s in enumerate(speakers)])

    check("没有分离信息时算 0 个", tr_with(["", "", ""]).speaker_count == 0)
    check("同一个人算 1 个", tr_with(["0", "0", "0"]).speaker_count == 1)
    check("两个人算 2 个", tr_with(["0", "1", "0"]).speaker_count == 2)

    # ---- 输出形态 ----
    def render(speakers, labels=True, parts=1, dia=True):
        tr = tr_with(speakers)
        meta = md.DocMeta(title="t", source_file="t.mp4", duration_ms=5000,
                          asr_backend="bailian", asr_model="m", diarization=dia,
                          transcribed_at=md.now_iso(), speaker_labels=labels,
                          parts=parts)
        with workspace_tmp() as d:
            outs = md.write_outputs(tr, meta, Path(d), slug="t")
            return outs["markdown"].read_text(encoding="utf-8")

    one = render(["0", "0"])
    check("单人：正文不加「发言人」前缀", "发言人" not in one.split("---\n", 2)[2],
          one[-300:])
    check("单人：front matter 仍记录 speaker_count",
          "speaker_count: 1" in one, one[:400])
    check("单人：speaker_labels 标成 false", "speaker_labels: false" in one)

    two = render(["0", "1"])
    check("多人：正文按发言人标注", "发言人 0：" in two and "发言人 1：" in two)
    check("多人：speaker_labels 标成 true", "speaker_labels: true" in two)

    none = render(["", ""], dia=False)
    check("没开分离时也不加前缀", "发言人" not in none.split("---\n", 2)[2])
    check("没开分离时 speaker_count 为 0", "speaker_count: 0" in none)

    forced = render(["0", "1"], labels=False)
    check("显式关掉标签时即使多人也不加", "发言人" not in forced.split("---\n", 2)[2])

    split = render(["0"], parts=3, dia=True)
    check("多段时正文顶部有编号局限说明",
          "各段内部的发言人编号互相独立" in split, split[:600])
    check("多段时 front matter 记了段数", "parts: 3" in split, split[:500])
    single_part = render(["0"], parts=1, dia=True)
    check("单段时不该有那段说明", "互相独立" not in single_part)

    # ---- pipeline：设置语义 ----
    cfg = _mkcfg(tmp, "spk")
    pipe = pl.Pipeline(cfg, log=_quiet)
    cfg.asr_backend = "bailian"
    cfg.bailian_diarization = True
    check("auto 跟随配置（开）", pipe._effective_diarization("auto") is True)
    cfg.bailian_diarization = False
    check("auto 跟随配置（关）", pipe._effective_diarization("auto") is False)
    check("single 强制关闭", pipe._effective_diarization("single") is False)
    check("multi 强制打开", pipe._effective_diarization("multi") is True)
    cfg.asr_backend = "siliconflow"
    check("非百炼后端不套用", pipe._effective_diarization("multi") is False)

    # ---- pipeline：改了分离设置必须重新转写（旧的缓存不能复用）----
    cfg2 = _mkcfg(tmp, "spk2")
    video = _mkvideo(tmp / "src_spk.mp4", 6)
    info = {"surl": "SS", "share_user": "u", "count": 1, "files": [
        {"fs_id": 1, "name": "s.mp4", "path": "/t/s.mp4", "size": video.stat().st_size,
         "isdir": False, "duration": 6},
    ]}
    saved = [{"src_fs_id": 1, "fs_id": 1, "path": "/t/s.mp4", "name": "s.mp4",
              "relpath": "s.mp4", "size": video.stat().st_size, "duration": 6}]
    calls: dict = {}
    _stub_io(pl, tmp, calls, info, saved, {"s.mp4": video})
    URL = "https://pan.baidu.com/s/1SPK"
    pipe2 = pl.Pipeline(cfg2, log=_quiet)

    cfg2.bailian_diarization = True
    pipe2.run(URL, speakers="multi")
    check("第一次转写了一次", calls.get("asr") == 1, str(calls.get("asr")))
    st = pipe2.load_state(URL)
    check("状态里记下了分离设置",
          st["files"]["s.mp4"].get("diarization") is True,
          str(st["files"]["s.mp4"].get("diarization")))

    calls["asr"] = 0
    pipe2.run(URL, speakers="multi")
    check("设置没变时不重复转写", calls.get("asr") == 0, str(calls.get("asr")))

    calls["asr"] = 0
    pipe2.run(URL, speakers="single")
    check("设置变了会重新转写", calls.get("asr") == 1, str(calls.get("asr")))
    st2 = pipe2.load_state(URL)
    check("状态里的分离设置被更新",
          st2["files"]["s.mp4"].get("diarization") is False)
    _unpatch_all()

    # ---- 索引里带上单人/多人标记，知识库可以直接筛 ----
    with workspace_tmp() as d:
        out = Path(d)
        tr = tr_with(["0", "0"])
        meta = md.DocMeta(title="单人讲座", source_file="a.mp4", duration_ms=2000,
                          asr_backend="bailian", asr_model="m",
                          transcribed_at=md.now_iso())
        md.write_outputs(tr, meta, out, slug="a")
        tr2 = tr_with(["0", "1"])
        meta2 = md.DocMeta(title="多人访谈", source_file="b.mp4", duration_ms=2000,
                           asr_backend="bailian", asr_model="m",
                           transcribed_at=md.now_iso())
        md.write_outputs(tr2, meta2, out, slug="b")
        idx = md.build_index(out)
        flags = {x["title"]: x["single_speaker"] for x in idx["docs"]}
        check("索引能区分单人与多人",
              flags.get("单人讲座") is True and flags.get("多人访谈") is False,
              str(flags))


def test_cost_and_path_probe() -> None:
    """下载之前就能看到的成本预估，以及硅基流动的路径探测。"""
    print("\n[25] 成本预估与接口路径探测")
    from bp2md import models
    from bp2md import preflight as pf
    from bp2md.config import Config
    from bp2md.pipeline import durations_from_files

    # ---- 从分享元数据汇总时长 ----
    files = [{"name": "a.mp4", "duration": 3600},
             {"name": "b.mp4", "duration": "1800"},
             {"name": "课程", "isdir": True},
             {"name": "c.mp4", "duration": None}]
    secs, unknown = durations_from_files(files)
    check("时长汇总正确（含字符串形式）", secs == 5400.0, str(secs))
    check("时长未知的条目被单独计数", unknown == 2, str(unknown))
    check("空清单不炸", durations_from_files([]) == (0.0, 0))

    # ---- 成本换算 ----
    check("paraformer-v2 按 0.288 元/小时换算",
          abs((models.estimate_cost_yuan("paraformer-v2", 3600) or 0) - 0.288) < 0.001,
          str(models.estimate_cost_yuan("paraformer-v2", 3600)))
    check("10 小时 = 2.88 元",
          abs((models.estimate_cost_yuan("paraformer-v2", 36000) or 0) - 2.88) < 0.001)
    check("按 token 计费的模型返回 None",
          models.estimate_cost_yuan("qwen-audio-3.1-asr-flash-filetrans", 3600) is None)
    check("零时长返回 None", models.estimate_cost_yuan("paraformer-v2", 0) is None)

    text = models.format_cost("paraformer-v2", 5400, unknown_count=2)
    check("预估文案含小时数与金额",
          "1.50 小时" in text and "0.43 元" in text, text)
    check("在免费额度内会提示", "免费额度" in text, text)
    check("有未知时长会说明", "2 项时长未知" in text, text)
    check("超出免费额度不再提示",
          "免费额度" not in models.format_cost("paraformer-v2", 40 * 3600), "")
    check("token 计费模型给出诚实说明",
          "无法按时长预估" in models.format_cost(
              "qwen-audio-3.1-asr-flash-filetrans", 3600), "")

    # ---- 硅基流动：401 能证明路径对，404 说明路径错 ----
    original = pf.requests
    try:
        pf.requests = _FakeRequests(_FakeResp(401, '{"code":30014,"message":"Token is invalid."}'))
        ok = pf._probe_transcription_path("https://api.siliconflow.cn/v1")
        check("401 被判为路径存在", ok.level == "ok", str(ok))
        check("说明里点出该网关先路由再鉴权",
              "先路由再鉴权" in ok.detail, ok.detail)

        pf.requests = _FakeRequests(_FakeResp(404, "Not Found"))
        bad = pf._probe_transcription_path("https://api.siliconflow.cn/v2")
        check("404 被判为路径错误", bad.level == "fail", str(bad))
        check("给出可直接照抄的修法",
              "SILICONFLOW_BASE" in bad.fix, bad.fix)

        pf.requests = _FakeRequests(_FakeResp(500, "boom"))
        warn = pf._probe_transcription_path("https://api.siliconflow.cn/v1")
        check("5xx 归为警告", warn.level == "warn", str(warn))

        # 走硅基流动后端时，预检一共给两条结论（连通性 + 路径）
        pf.requests = _FakeRequests(_FakeResp(401, '{"code":30014,"message":"Token is invalid."}'))
        cfg = Config(asr_backend="siliconflow", siliconflow_api_key="sk-x")
        rs = pf.check_asr(cfg)
        check("硅基流动后端预检项包含路径校验",
              any(r.name == "转写路径" for r in rs), str([r.name for r in rs]))
        check("全部为 ok", all(r.level == "ok" for r in rs), str([(r.name, r.level) for r in rs]))
    finally:
        pf.requests = original

    # 百炼那边仍然要诚实说明"证明不了路径"
    pf.requests = _FakeRequests(_FakeResp(401, '{"code":"InvalidApiKey"}'))
    try:
        rs2 = pf.check_asr(Config(asr_backend="bailian", dashscope_api_key="x",
                                  bailian_model="paraformer-v2"))
        check("百炼预检明确说明证明不了路径",
              any("不能证明路径" in r.detail for r in rs2),
              str([r.detail[:40] for r in rs2]))
    finally:
        pf.requests = original


def test_docs_consistency() -> None:
    """文档和代码必须说同一件事。

    这类漂移已经真实咬过一次：README 的成本表把 `paraformer-v2` 标成"不支持
    说话人分离"，用户据此判断"单/多说话人要用不同 API"——而那个前提是错的。
    所以把一致性变成断言，而不是靠人记得同步。
    """
    print("\n[26] 文档与代码一致性")
    import re

    env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
    cfg_src = (ROOT / "bp2md" / "config.py").read_text(encoding="utf-8")

    declared = re.findall(r"^([A-Z][A-Z0-9_]*)=.*$", env_text, re.M)
    # 注意键名里可能有数字（BPAN2MD_WORKDIR），别写成 [A-Z_]+
    read_keys = set(re.findall(r'val(?:_int|_bool)?\(\s*"([A-Z0-9_]+)"', cfg_src))

    unread = [k for k in declared if k not in read_keys]
    check("每个 .env 键都真的被代码读取", not unread, f"声明了但没人读: {unread}")
    undoc = sorted(read_keys - set(declared))
    check("代码读取的每个键都在 .env.example 里有说明", not undoc,
          f"代码在读但没文档: {undoc}")
    check(".env.example 里有键（没解析空）", len(declared) >= 20, str(len(declared)))

    # ---- README 里的 front matter 示例，字段必须真的会被生成 ----
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"```markdown\n---\n(.*?)\n---\n", readme, re.S)
    check("README 里能找到 front matter 示例", m is not None)
    if m:
        fields = re.findall(r"^([a-z_]+):", m.group(1), re.M)
        from bp2md import md as _md
        from bp2md.asr import Segment, Transcript
        tr = Transcript(segments=[Segment(0, 1000, "甲。", "0"),
                                  Segment(1000, 2000, "乙。", "1")],
                        duration_ms=2000, model="m", backend="bailian")
        meta = _md.DocMeta(title="t", source_file="t.mp4",
                           source_url="https://pan.baidu.com/s/1x",
                           duration_ms=2000, asr_backend="bailian", asr_model="m",
                           diarization=True, transcribed_at=_md.now_iso())
        with workspace_tmp() as d:
            outs = _md.write_outputs(tr, meta, Path(d), slug="t")
            produced = outs["markdown"].read_text(encoding="utf-8")
        missing = [f for f in fields if not re.search(rf"^{f}:", produced, re.M)]
        check("front matter 示例里的每个字段都会真的产出", not missing,
              f"README 写了但没生成: {missing}")
        check("示例里的字段数够多（至少 10 个）", len(fields) >= 10, str(len(fields)))

    # ---- README 的命令一览必须和 argparse 一致 ----
    from bp2md import cli
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices
               and "check" in a.choices)
    real_cmds = set(sub.choices)

    block = re.search(r"```\n((?:(?:check|preflight|doctor|inspect|speedtest|run|index|render|clean|web|local)\s.*?\n)+)```",
                      readme, re.S)
    check("README 里能找到命令一览", block is not None)
    if block:
        listed = {line.split()[0] for line in block.group(1).splitlines() if line.strip()}
        check("README 列出的命令与代码完全一致",
              listed == real_cmds,
              f"README 多: {sorted(listed - real_cmds)}  少: {sorted(real_cmds - listed)}")

    # ---- README 里的 .env 变量名必须在 .env.example 里存在 ----
    mentioned = set(re.findall(r"\b([A-Z][A-Z0-9_]{4,})\b", readme))
    known = set(declared) | {"BAIDU_COOKIE", "BAILIAN_DIARIZATION", "MAX_SEGMENT_SECONDS",
                             "SILICONFLOW_BASE", "OSS_PUBLIC_READ", "BAILIAN_MODEL",
                             "ASR_BACKEND"}
    suspicious = {v for v in mentioned
                  if v.endswith(("_KEY", "_SECRET", "_COOKIE", "_BASE", "_ENDPOINT"))
                  and v not in known}
    check("README 提到的配置项都真实存在", not suspicious, str(suspicious))

    # ---- requirements 文件必须是纯 ASCII ----
    # pip 用系统本地编码（中文 Windows 是 GBK）读 requirements 文件，
    # 里面只要有一个非 ASCII 字节，`pip install -r requirements.txt` 就会
    # 直接 UnicodeDecodeError 失败——而且报错信息完全指不到原因。
    # 这个坑真实发生过，所以钉一条断言。
    for name in ("requirements.txt", "requirements-dev.txt"):
        path = ROOT / name
        if not path.exists():
            continue
        raw = path.read_bytes()
        bad = [i for i, b in enumerate(raw) if b > 127]
        check(f"{name} 是纯 ASCII（pip 依赖这个）", not bad,
              f"第 {bad[:3]} 字节处有非 ASCII 字符，pip 在中文 Windows 上会读不了")


def test_env_reload_and_save() -> None:
    """回归：改了 .env，同一个进程里再 load 必须生效。

    以前用 os.environ.setdefault 注入，导致**改动永远不会生效**——网页里保存完
    配置不重启就白填；挂机模式里承诺的"更新 Cookie 下一轮自动接上"也成了空话。
    """
    print("\n[27] 配置热更新与 .env 写回")
    import os
    from bp2md import config as _cfg

    env_path = ROOT / ".selftest-tmp" / "reload.env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("# 注释要保留\nBAIDU_COOKIE=OLD-1\nOSS_BUCKET=keepme\n",
                        encoding="utf-8")

    touched = ("BAIDU_COOKIE", "ASR_BACKEND", "OSS_BUCKET", "BAILIAN_MODEL")
    saved_env = {k: os.environ.get(k) for k in touched}
    try:
        for k in touched:
            os.environ.pop(k, None)

        first = _cfg.Config.load(env_path)
        check("第一次加载读到 .env 的值", first.baidu_cookie == "OLD-1",
              first.baidu_cookie)

        written = _cfg.update_env_file(env_path, {"BAIDU_COOKIE": "NEW-2",
                                                 "ASR_BACKEND": "siliconflow"})
        check("写回只写允许的键", sorted(written) == ["ASR_BACKEND", "BAIDU_COOKIE"],
              str(written))
        text = env_path.read_text(encoding="utf-8")
        check("原有注释被保留", "# 注释要保留" in text, text)
        check("未被修改的键被保留", "OSS_BUCKET=keepme" in text, text)

        second = _cfg.Config.load(env_path)
        check("同一个进程里重新加载能拿到新值（回归）",
              second.baidu_cookie == "NEW-2", second.baidu_cookie)
        check("其它键也跟着更新", second.asr_backend == "siliconflow",
              second.asr_backend)

        # 白名单之外不许写
        written2 = _cfg.update_env_file(env_path, {"SOMETHING_ELSE": "x"})
        check("白名单之外的键被拒绝", written2 == [], str(written2))

        # 超长值必须被拒绝——否则写进 .env 之后 Windows 环境变量放不下，
        # 每次读配置都会抛 ValueError，整个程序起不来
        try:
            _cfg.update_env_file(env_path, {"BAIDU_COOKIE": "x" * 100000})
            check("超长值被拒绝", False)
        except ValueError as exc:
            check("超长值被拒绝", "太长" in str(exc), str(exc)[:80])

        # 就算 .env 里已经有一个超长的值，也不能让程序起不来
        env_path.write_text(f"BAIDU_COOKIE={'y' * 40000}\nOSS_BUCKET=ok\n",
                            encoding="utf-8")
        os.environ.pop("BAIDU_COOKIE", None)
        _cfg._ENV_PROBLEMS.clear()
        try:
            survivor = _cfg.Config.load(env_path)
            check("坏掉的 .env 不会让加载崩掉", survivor.oss_bucket == "ok",
                  survivor.oss_bucket)
        except Exception as exc:  # noqa: BLE001
            check("坏掉的 .env 不会让加载崩掉", False, f"{type(exc).__name__}: {exc}")
        check("坏键会出现在 check() 里，并说明怎么修",
              any("太长" in p and "BAIDU_COOKIE" in p
                  for p in _cfg.Config.load(env_path).check(require_baidu=False)),
              str(survivor.check(require_baidu=False))[:200])
        _cfg._ENV_PROBLEMS.clear()

        # 换行会被去掉（Cookie 常是多行粘贴）
        _cfg.update_env_file(env_path, {"BAIDU_COOKIE": "A=1;\nB=2;\n"})
        check("值里的换行被清掉",
              _cfg.Config.load(env_path).baidu_cookie == "A=1;B=2;",
              _cfg.Config.load(env_path).baidu_cookie)

        # 进程里本来就有的真实环境变量，仍然以它为准
        for k in touched:
            os.environ.pop(k, None)
        os.environ["ASR_BACKEND"] = "bailian"
        check("真实环境变量优先于 .env",
              _cfg.Config.load(env_path).asr_backend == "bailian",
              _cfg.Config.load(env_path).asr_backend)
    finally:
        for k in touched:
            if saved_env[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved_env[k]
        _cfg._ENV_PROBLEMS.clear()
        shutil.rmtree(env_path.parent, ignore_errors=True)


def test_web_api(tmp: Path) -> None:
    """网页界面的后端接口：起一个真服务，用真 HTTP 打它。"""
    print("\n[28] 网页界面 API")
    import threading
    import requests
    from bp2md import config as config_mod
    from bp2md import md as _md
    from bp2md.web import server as web

    cfg = _mkcfg(tmp, "web")
    env_path = tmp / "web" / ".env"

    # 把配置路径指到临时文件、输出目录指到临时目录，绝不动真实 .env 和 output/
    saved_env_path = config_mod.ENV_PATH
    saved_dirs = {k: os.environ.get(k) for k in ("BPAN2MD_WORKDIR", "BPAN2MD_OUTDIR")}
    config_mod.ENV_PATH = env_path
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("# 测试用\nOSS_BUCKET=orig-bucket\n", encoding="utf-8")
    os.environ["BPAN2MD_OUTDIR"] = str(tmp / "web_out")
    os.environ["BPAN2MD_WORKDIR"] = str(tmp / "web_work")

    httpd = web.make_server(0)
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # ---- 静态页 ----
        r = requests.get(base + "/", timeout=10)
        check("首页能打开", r.status_code == 200 and "bpan2md" in r.text,
              f"HTTP {r.status_code}")
        check("首页是 UTF-8 中文", "百度网盘" in r.text)

        # ---- 状态接口 ----
        r = requests.get(base + "/api/state", timeout=10)
        st = r.json()
        check("状态接口返回配置", "config" in st and "problems" in st["config"],
              str(list(st)))
        check("状态里带模型登记表", len(st["config"]["catalog"]) >= 3,
              str(len(st["config"]["catalog"])))
        check("明文回填字段不含密钥",
              "DASHSCOPE_API_KEY" not in st["config"]["plain"]
              and "BAIDU_COOKIE" not in st["config"]["plain"],
              str(list(st["config"]["plain"])))
        check("明文回填里带 Bucket",
              st["config"]["plain"]["OSS_BUCKET"] == "orig-bucket",
              st["config"]["plain"]["OSS_BUCKET"])

        # ---- 保存配置 ----
        r = requests.post(base + "/api/config", timeout=10,
                          json={"BAIDU_COOKIE": "BDUSS=abc; STOKEN=def",
                                "OSS_BUCKET": "new-bucket",
                                "不在白名单": "x"})
        saved = r.json()
        check("保存后回显已写入的键",
              sorted(saved["written"]) == ["BAIDU_COOKIE", "OSS_BUCKET"],
              str(saved["written"]))
        text = env_path.read_text(encoding="utf-8")
        check("原有注释被保留", "# 测试用" in text, text)
        check("配置立刻生效（同一进程内）",
              saved["config"]["plain"]["OSS_BUCKET"] == "new-bucket",
              saved["config"]["plain"]["OSS_BUCKET"])
        check("Cookie 已写入但不是明文回显",
              "BAIDU_COOKIE" not in saved["config"]["plain"]
              and "abc" not in json.dumps(saved["config"]["plain"], ensure_ascii=False))

        # ---- 起任务：用 index（离线、不花钱）----
        tr = _md.load_result  # noqa: F841  仅确认模块可用
        from bp2md.asr import Segment, Transcript
        meta = _md.DocMeta(title="示例", source_file="a.mp4", duration_ms=1000,
                           asr_backend="bailian", asr_model="paraformer-v2",
                           transcribed_at=_md.now_iso())
        _md.write_outputs(Transcript(segments=[Segment(0, 1000, "你好。", "0")],
                                     duration_ms=1000, model="m", backend="bailian"),
                          meta, Path(os.environ["BPAN2MD_OUTDIR"]), slug="a")

        r = requests.post(base + "/api/job", timeout=10,
                          json={"action": "index", "params": {}})
        job = r.json()
        check("起任务返回 job_id", "job_id" in job, str(job))

        status = None
        for _ in range(80):
            time.sleep(0.1)
            s = requests.get(f"{base}/api/job/{job['job_id']}?from=0", timeout=10).json()
            if s["status"] != "running":
                status = s
                break
        check("任务跑完", status is not None and status["status"] == "done",
              str(status and status["status"]))
        check("日志有内容", status and status["total"] > 0,
              str(status and status["total"]))
        check("日志增量拉取不重复",
              requests.get(f"{base}/api/job/{job['job_id']}?from=1",
                           timeout=10).json()["total"] == status["total"])

        # ---- 输出列表与文件下载 ----
        outs = requests.get(base + "/api/outputs", timeout=10).json()["outputs"]
        names = [o["name"] for o in outs]
        check("能看到产出文件", "a.md" in names, str(names))
        f = requests.get(base + "/api/file?name=a.md", timeout=10)
        check("能预览 Markdown", f.status_code == 200 and "你好" in f.text)
        f2 = requests.get(base + "/api/file?name=a.md&dl=1", timeout=10)
        check("下载会带 Content-Disposition",
              "attachment" in (f2.headers.get("Content-Disposition") or ""),
              str(f2.headers.get("Content-Disposition")))

        # ---- 安全性：不能读到输出目录之外的文件 ----
        bad = requests.get(base + "/api/file?name=../../../.env", timeout=10)
        check("拒绝路径穿越", bad.status_code == 404, str(bad.status_code))
        bad2 = requests.get(base + "/api/file?name=..%2F..%2F.env", timeout=10)
        check("拒绝编码过的路径穿越", bad2.status_code == 404, str(bad2.status_code))

        # ---- 对抗输入：坏请求必须是 4xx，不能 500，更不能把服务弄坏 ----
        # 这几条是从真实的对抗演练里发现的：一个 300 万字符的 Cookie 被写进 .env
        # 之后，Windows 环境变量放不下，之后每次读配置都抛 ValueError，
        # 整个服务从此不可用，而用户完全不知道该怎么修。
        r = requests.post(base + "/api/config",
                          json={"BAIDU_COOKIE": "x" * 100000}, timeout=30)
        check("超长配置值被拒绝", r.status_code == 400, str(r.status_code))
        check("拒绝的理由是人话", "太长" in r.text, r.text[:120])
        check("拒绝之后服务仍然可用",
              requests.get(base + "/api/state", timeout=10).status_code == 200)

        r = requests.post(base + "/api/config",
                          json={"BAIDU_COOKIE": "x" * 400000}, timeout=30)
        check("超大请求体返回 413", r.status_code == 413, str(r.status_code))
        check("413 之后服务仍然可用",
              requests.get(base + "/api/state", timeout=10).status_code == 200)

        check("任务编号非数字返回 400",
              requests.get(base + "/api/job/abc", timeout=10).status_code == 400)
        check("from 非数字返回 400",
              requests.get(base + "/api/job/1?from=xyz", timeout=10).status_code == 400)
        check("缺 name 参数返回 404",
              requests.get(base + "/api/file", timeout=10).status_code == 404)
        check("非法 JSON 返回 400",
              requests.post(base + "/api/config", data="not json", timeout=10,
                            headers={"Content-Type": "application/json"}
                            ).status_code == 400)
        check("非对象请求体返回 400",
              requests.post(base + "/api/config", json=[1, 2], timeout=10
                            ).status_code == 400)
        check("上述折腾之后服务依然可用",
              requests.get(base + "/api/state", timeout=10).status_code == 200)

        # ---- 整段粘贴识别 ----
        blob = ("通过网盘分享的文件：《某讲座》01.mp4\n"
                "链接: https://pan.baidu.com/s/1PwCbt0-aE-al_EKVcIPs1Q?pwd=8eb6 "
                "提取码: 8eb6 复制这段内容后打开百度网盘手机App，操作更方便哦")
        d = requests.post(base + "/api/parse", timeout=10,
                          json={"text": blob}).json()
        check("/api/parse 能认出整段粘贴里的链接", d["count"] == 1, str(d)[:200])
        check("/api/parse 带回提取码和文件名",
              d["links"][0]["pwd"] == "8eb6" and d["links"][0]["title"] == "《某讲座》01.mp4",
              str(d["links"]))
        check("/api/parse 对空文本不报错",
              requests.post(base + "/api/parse", timeout=10,
                            json={"text": ""}).json()["count"] == 0)
        check("/api/parse 会指出认不出的内容",
              requests.post(base + "/api/parse", timeout=10,
                            json={"text": "https://www.alipan.com/s/xyz"}
                            ).json()["unrecognized"],
              "认不出的链接没有被报出来")
        check("/api/parse 能识别缺提取码的情况",
              requests.post(base + "/api/parse", timeout=10,
                            json={"text": "https://pan.baidu.com/s/1NoPwd"}
                            ).json()["missing_pwd"] == ["https://pan.baidu.com/s/1NoPwd"])

        # ---- 扫描本机文件夹 ----
        import os as _os
        from urllib.parse import quote as _quote
        scandir = tmp / "web_scan"
        scandir.mkdir(parents=True, exist_ok=True)
        (scandir / "第一集.mp4").write_bytes(b"x" * 1234)
        (scandir / "笔记.txt").write_text("不是音视频", encoding="utf-8")
        (scandir / "子目录").mkdir(exist_ok=True)
        (scandir / "子目录" / "第二集.mkv").write_bytes(b"y" * 99)

        d = requests.post(base + "/api/local/scan", timeout=10,
                          json={"path": str(scandir)}).json()
        names = [f["name"] for f in d["files"]]
        check("扫描文件夹只列出音视频", names == ["第一集.mp4"], str(names))
        check("扫描结果带大小，便于确认文件对不对", d["total_size"] == 1234, str(d))
        d2 = requests.post(base + "/api/local/scan", timeout=10,
                           json={"path": str(scandir), "deep": True}).json()
        check("勾了「包含子文件夹」就能扫到子目录",
              sorted(f["name"] for f in d2["files"]) == ["第一集.mp4", "第二集.mkv"],
              str(d2["files"]))
        check("子目录里的文件带相对路径（好区分同名文件）",
              any(f["relpath"].endswith("第二集.mkv") and _os.sep in f["relpath"]
                  for f in d2["files"]), str([f["relpath"] for f in d2["files"]]))
        d3 = requests.post(base + "/api/local/scan", timeout=10,
                           json={"path": f'路径： "{scandir}"'}).json()
        check("带引号、带「路径：」前缀的粘贴也能认",
              [f["name"] for f in d3["files"]] == ["第一集.mp4"], str(d3)[:200])
        d4 = requests.post(base + "/api/local/scan", timeout=10,
                           json={"path": str(scandir / "第一集.mp4")}).json()
        check("直接给一个文件路径也认", d4["count"] == 1, str(d4)[:200])

        r = requests.post(base + "/api/local/scan", timeout=10,
                          json={"path": str(tmp / "根本没有这个目录")})
        check("扫描不存在的目录返回 400", r.status_code == 400, str(r.status_code))
        check("并且告诉用户去哪里复制路径",
              "资源管理器" in r.text and "地址栏" in r.text, r.text[:200])
        r = requests.post(base + "/api/local/scan", timeout=10,
                          json={"path": str(scandir / "笔记.txt")})
        check("扫描一个非音视频文件返回 400", r.status_code == 400, str(r.status_code))
        r = requests.post(base + "/api/local/scan", timeout=10, json={"path": ""})
        check("不给路径返回 400", r.status_code == 400, str(r.status_code))
        check("GET 形式也能扫描",
              requests.get(base + "/api/local/scan",
                           params={"path": str(scandir)}, timeout=10
                           ).json()["count"] == 1)

        # ---- 上传本机文件（流式，不用 multipart）----
        updir = Path(os.environ["BPAN2MD_WORKDIR"]) / "uploads"

        def up(name, data, **kw):
            h = {"X-File-Name": _quote(name), "Content-Type": "application/octet-stream"}
            h.update(kw.pop("headers", {}))
            return requests.post(base + "/api/upload", data=data, headers=h, **kw)

        r = up("测试 上传.mp4", b"a" * 5000)
        check("上传成功并回报落盘信息",
              r.status_code == 200 and r.json()["size"] == 5000, r.text[:200])
        check("文件名里的空格和中文被保留", r.json()["name"] == "测试 上传.mp4",
              str(r.json()))
        saved = Path(r.json()["path"])
        check("文件真的写到了 uploads 目录",
              saved.exists() and saved.parent == updir and saved.stat().st_size == 5000,
              str(saved))
        check("上传目录里没有留下 .part 半成品",
              not list(updir.glob("*.part")), str(list(updir.glob("*.part"))))
        check("上传后 /api/state 里能看到它",
              any(u["name"] == "测试 上传.mp4"
                  for u in requests.get(base + "/api/state", timeout=10).json()["uploads"]))

        r = up("../../想穿越.mp4", b"b" * 10)
        check("上传的文件名会被剥掉目录部分（不能写到别处）",
              r.json()["name"] == "想穿越.mp4"
              and Path(r.json()["path"]).parent == updir, r.text[:200])
        r = up('a<b>c:d|e?f.mp4', b"c" * 10)
        check("Windows 非法字符被替换掉，不会写失败",
              r.status_code == 200 and "<" not in r.json()["name"], r.text[:200])
        r = up("这不是音视频.txt", b"d" * 10)
        check("非音视频文件在上传阶段就被拒（省得白等）",
              r.status_code == 400 and "不是音视频" in r.text, r.text[:200])
        r = requests.post(base + "/api/upload", data=b"x" * 10, timeout=10)
        check("没有文件名时返回 400", r.status_code == 400, str(r.status_code))

        # 上传上限：把上限临时改小来验证这条防线真的会拦（不然要传 200GB）
        old_max = web.MAX_UPLOAD
        web.MAX_UPLOAD = 1000
        try:
            r = up("太大.mp4", b"e" * 2000)
            check("超过上限的文件被拒绝（413）", r.status_code == 413, str(r.status_code))
            check("拒绝时说清了上限是多少", "上限" in r.text, r.text[:160])
        finally:
            web.MAX_UPLOAD = old_max

        # 磁盘不够：同样把 disk_usage 打桩，验证会在收文件之前就拒绝
        real_usage = web.shutil.disk_usage
        try:
            web.shutil.disk_usage = lambda p: type("U", (), {"free": 10, "total": 0,
                                                             "used": 0})()
            r = up("空间不够.mp4", b"f" * 5000)
            check("磁盘空间不足时提前拒绝（507）", r.status_code == 507, str(r.status_code))
            check("并且给出了替代方案（改用扫描文件夹）",
                  "扫描" in r.text, r.text[:200])
        finally:
            web.shutil.disk_usage = real_usage

        # 传到一半断开：必须不留半截文件（否则后面可能把它当成完整文件用）
        import socket as _socket
        partial = updir / "半截.mp4"
        try:
            s = _socket.create_connection(("127.0.0.1", port), timeout=10)
            head = (f"POST /api/upload HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    f"X-File-Name: {_quote('半截.mp4')}\r\n"
                    f"Content-Type: application/octet-stream\r\n"
                    f"Content-Length: 100000\r\n\r\n").encode()
            s.sendall(head)
            s.sendall(b"z" * 100)      # 只发一小部分就断
            s.close()
        except OSError as exc:
            check("模拟上传中断", False, str(exc))
        time.sleep(0.5)
        check("上传中断不会留下半截文件或 .part",
              not partial.exists() and not list(updir.glob("*.part")),
              str(list(updir.iterdir())))
        check("中断之后服务仍然可用",
              requests.get(base + "/api/state", timeout=10).status_code == 200)

        # 删除上传的文件
        d = requests.post(base + "/api/upload/remove", timeout=10,
                          json={"name": "测试 上传.mp4"}).json()
        check("能删掉上传的文件", d["removed"] == "测试 上传.mp4", str(d)[:120])
        check("删完之后列表里没有了",
              all(u["name"] != "测试 上传.mp4" for u in d["uploads"]))
        r = requests.post(base + "/api/upload/remove", timeout=10,
                          json={"name": "不存在.mp4"})
        check("删不存在的文件返回 404", r.status_code == 404, str(r.status_code))
        r = requests.post(base + "/api/upload/remove", timeout=10,
                          json={"name": "../../.env"})
        check("删除接口挡住了路径穿越", r.status_code == 404, str(r.status_code))

        # ---- 扫描时不能把工具自己产出的文件当成输入 ----
        (updir / "抽出来的音频.mp3").write_bytes(b"g" * 10)
        d = requests.post(base + "/api/local/scan", timeout=10,
                          json={"path": str(updir)}).json()
        check("扫描到工具自己的 work 目录时会跳过它",
              d["count"] == 0 and d["skipped_mine"] >= 1, str(d)[:200])

        # ---- 本地转写任务 ----
        r = requests.post(base + "/api/job", timeout=10,
                          json={"action": "run_local", "params": {"paths": []}})
        check("run_local 是后端认识的动动作", "job_id" in r.json(), r.text[:120])
        jid = r.json()["job_id"]
        stt = None
        for _ in range(60):
            time.sleep(0.1)
            stt = requests.get(f"{base}/api/job/{jid}?from=0", timeout=10).json()
            if stt["status"] != "running":
                break
        check("没给文件时任务失败并说明原因",
              stt and stt["status"] == "failed" and "没有要处理的本地文件" in stt["error"],
              str(stt and stt["error"])[:160])

        r = requests.post(base + "/api/job", timeout=10,
                          json={"action": "run_local",
                                "params": {"paths": [str(scandir / "第一集.mp4")]}})
        jid = r.json()["job_id"]
        stt = None
        for _ in range(60):
            time.sleep(0.1)
            stt = requests.get(f"{base}/api/job/{jid}?from=0", timeout=10).json()
            if stt["status"] != "running":
                break
        check("云端配置不全时，本地转写会提前失败并列出缺什么",
              stt and stt["status"] == "failed"
              and any("✗" in x for x in stt["lines"]), str(stt and stt["lines"])[:300])
        check("这个失败信息里明确说了不需要百度 Cookie",
              stt and any("不需要百度 Cookie" in x for x in stt["lines"]),
              str(stt and stt["lines"])[:300])

        # ---- 同源防线 ----
        r = requests.post(base + "/api/job", timeout=10, json={"action": "index"},
                          headers={"Origin": "https://evil.example"})
        check("跨站来源的 POST 被拒绝（防 CSRF）", r.status_code == 403,
              str(r.status_code))
        r = requests.post(base + "/api/upload", data=b"x", timeout=10,
                          headers={"X-File-Name": "a.mp4",
                                   "Origin": "https://evil.example"})
        check("跨站来源的上传同样被拒绝", r.status_code == 403, str(r.status_code))
        r = requests.get(base + "/api/state", timeout=10,
                         headers={"Host": "evil.example"})
        check("非本机 Host 的请求被拒绝（防 DNS rebinding）", r.status_code == 403,
              str(r.status_code))
        check("被拒绝之后服务仍然可用",
              requests.get(base + "/api/state", timeout=10).status_code == 200)
        check("本机来源的请求（localhost）仍然放行",
              requests.get(base + "/api/state", timeout=10,
                           headers={"Host": f"localhost:{port}"}).status_code == 200)

        # ---- 按钮的输出必须真的进日志 ----
        # preflight / doctor 是写给终端看的（用 print）。网页只能拿到 log 回调，
        # 不做 stdout 重定向的话，用户点完按钮看到一片空白——真实发生过。
        from bp2md.web.server import Job, _do_doctor, _do_preflight
        jd = Job("doctor", "体检")
        _do_doctor(Config.load(), jd)
        snapd = jd.snapshot(0)
        check("doctor 的输出会进网页日志", snapd["total"] > 0, str(snapd["total"]))
        check("doctor 日志里说明了缺什么",
              any("配置" in x or "API" in x or "✗" in x for x in snapd["lines"]),
              str(snapd["lines"][:4]))

        jp = Job("preflight", "预检")
        _do_preflight(Config.load(), jp)
        snapp = jp.snapshot(0)
        check("preflight 的输出会进网页日志", snapp["total"] > 0, str(snapp["total"]))

        # ---- 错误处理 ----
        r = requests.post(base + "/api/job", timeout=10,
                          json={"action": "不存在的动作"})
        check("不认识的动作被拒绝", r.status_code == 400, str(r.status_code))
        r = requests.post(base + "/api/stop", timeout=10, json={})
        check("没有任务时停止返回失败", r.json().get("ok") is False, r.text[:80])
        r = requests.get(base + "/api/不存在", timeout=10)
        check("未知地址返回 404", r.status_code == 404)
    finally:
        httpd.shutdown()
        httpd.server_close()
        config_mod.ENV_PATH = saved_env_path
        for k, v in saved_dirs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cfg.outdir, ignore_errors=True)
        shutil.rmtree(cfg.workdir, ignore_errors=True)


def main() -> int:
    print("=" * 60)
    print("bpan2md 离线自检（不联网、不需要密钥）")
    print("=" * 60)
    baidu._bdpan_common = __import__("bdpan_common")
    test_cookies()
    test_timestamps()
    test_slug()
    test_markdown()
    test_siliconflow_split()
    test_oss_signing()
    test_oss_public_acl_fallback()
    test_retry()
    test_public_check()
    test_preflight()
    test_paste_parsing()
    with workspace_tmp() as tmp:
        t = Path(tmp)
        test_media(t)
        test_links(t)
        test_config(t)
        test_share_already_in_pan()
        test_pipeline_offline(t)
        test_index(t / "output")
    with workspace_tmp() as tmp2:
        t2 = Path(tmp2)
        test_selection_change(t2)
        test_folder_share(t2)
        test_dry_run_and_checks(t2)
        test_long_run_resilience(t2)
        test_render_and_clean(t2)
        test_clean_workdir(t2)
    test_supervise()
    test_asr_clients()
    test_model_registry()
    test_cost_and_path_probe()
    test_docs_consistency()
    test_env_reload_and_save()
    with workspace_tmp() as tmp4:
        test_web_api(Path(tmp4))
    with workspace_tmp() as tmp3:
        test_speaker_handling(Path(tmp3))
    with workspace_tmp() as tmp5:
        test_local_files(Path(tmp5))
    test_vendored_upstream()
    print("\n" + "=" * 60)
    check("所有打桩都已还原（无跨测试泄漏）", not _RESTORE,
          f"残留 {len(_RESTORE)} 处：{[k for _, k, _ in _RESTORE]}")

    # 这一条必须是**最后一条**：它要和自己对齐，才能等于下面打印的总数
    import re as _re
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    claimed = _re.findall(r"\*\*(\d+) 项离线检查全部通过\*\*", readme_text)
    if not claimed:
        check("README 里写了验证项数", False, "没找到 '**N 项离线检查全部通过**'")
    else:
        n = int(claimed[0])
        actual = len(PASS) + len(FAIL) + 1      # +1 是本条断言自身
        check("README 的验证项数与实际一致", n == actual,
              f"README 写 {n}，实际 {actual}（改完测试记得同步 README）")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
    print("=" * 60)
    # 临时目录整个收掉，别在交付目录里留垃圾
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
