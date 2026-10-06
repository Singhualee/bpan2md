"""从「粘贴进来的任意文本」里认出百度网盘分享链接。

真实场景是从网盘 App 直接复制出来的整段，长这样：

    通过网盘分享的文件：《戎震谈男性成长》02：没有人会可怜你，你是弱者….mp4
    链接: https://pan.baidu.com/s/1PwCbt0-aE-al_EKVcIPs1Q?pwd=8eb6 提取码: 8eb6 复制这段内容后打开百度网盘手机App，操作更方便哦

用户要做的只是把这两行整段粘进来。**以前的做法是"一行当一个链接"**，于是
第一行（标题）被原样当成链接送去解析，报一个 `not a Baidu share URL`——
用户看到的是"标题那一行报错"，而真正的原因是这种输入格式压根没被支持过。

所以这里的思路反过来：**不假设输入长什么样**，而是在文本里"找"链接，
再把链接附近的提取码、文件名认出来。认不出的行会被单独列出来给用户看，
这样粘贴内容不对时（比如粘了一个别的网盘的链接）能立刻发现，而不是
悄悄少转几个文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- 链接形态 ---------------------------------------------------------------
# 现代：https://pan.baidu.com/s/1PwCbt0-aE-al_EKVcIPs1Q?pwd=8eb6
# 早期：https://pan.baidu.com/share/init?surl=PwCbt0...
# 协议头可能没有（部分 App 复制出来就是裸的 pan.baidu.com/s/1xxx）
_URL_RE = re.compile(
    r"(?:https?://)?(?:pan|yun|eyun)\.baidu\.com"
    r"(?:/s/1(?P<token>[0-9A-Za-z_-]+)"
    r"|/share/init\?(?:surl|shareid)=(?P<surl>[0-9A-Za-z_-]+))",
    re.I,
)

# ---- 提取码 ----------------------------------------------------------------
# 百度提取码固定 4 位。后面跟的 (?![0-9A-Za-z]) 很重要：
# 没有它的话，"提取码: 8eb6abc" 会被截成 "8eb6"，反而认错。
_PWD_QUERY_RE = re.compile(r"[?&]pwd=([0-9A-Za-z]{4})(?![0-9A-Za-z])")
_PWD_LABEL_RE = re.compile(
    r"(?:提取密码|提取码|访问码|密码|口令)\s*[:：=]?\s*([0-9A-Za-z]{4})(?![0-9A-Za-z])")
# 本项目自己的写法：链接 | 提取码（links.example.txt 里就是这么写的）
_PWD_PIPE_RE = re.compile(r"\|\s*([0-9A-Za-z]{4})(?![0-9A-Za-z])")

# ---- 文件名 ----------------------------------------------------------------
_TITLE_RE = re.compile(
    r"(?:通过网盘分享的文件|网盘分享的文件|分享的文件|文件名|文件)\s*[:：]\s*(.+)")

# 这些行是分享文本自带的套话，认不出来也正常，不该报给用户
_BOILERPLATE = ("复制这段内容", "打开百度网盘", "手机App", "手机app", "百度网盘手机")

_MAX_TEXT = 200_000        # 粘进来一份文件清单也不该有这么多字，防呆
_MAX_TITLE = 200


@dataclass
class Link:
    """一个认出来的分享链接。"""

    url: str                      # 规范化后的链接（一律带 https:// 和 /s/1）
    pwd: str = ""
    title: str = ""               # 粘贴文本里的文件名，仅用于显示
    line: int = 0                 # 在原文里的行号（从 1 开始，便于提示）

    def as_tuple(self) -> tuple[str, str]:
        return (self.url, self.pwd)


@dataclass
class Parsed:
    links: list[Link] = field(default_factory=list)
    unrecognized: list[str] = field(default_factory=list)
    duplicates: int = 0           # 同一段文本里重复出现的链接数

    @property
    def pairs(self) -> list[tuple[str, str]]:
        return [x.as_tuple() for x in self.links]

    def describe(self) -> str:
        """一句话总结识别结果，给界面和日志直接用。"""
        if not self.links:
            return "没认出百度网盘的分享链接"
        parts = [f"认出 {len(self.links)} 个分享链接"]
        if self.duplicates:
            parts.append(f"（{self.duplicates} 个重复的已忽略）")
        return "".join(parts)


@dataclass
class _Hit:
    line: int
    start: int
    end: int
    token: str


def _strip_leading_one(surl: str) -> str:
    """早期 /share/init?surl= 的值有的含前导 1、有的不含，统一成不含。

    规范链接长这样：https://pan.baidu.com/s/1<surl>。底层的解析函数就是按
    `/s/1([\\w-]+)` 取 surl 的，所以这里必须对齐到"不含前导 1"的形式。
    """
    return surl[1:] if surl.startswith("1") and len(surl) > 1 else surl


def _title_of(line: str) -> str:
    m = _TITLE_RE.search(line)
    if m:
        return m.group(1).strip().rstrip("，,;；")[:_MAX_TITLE]
    if "《" in line and "》" in line:
        return line[line.index("《"):line.rindex("》") + 1].strip()[:_MAX_TITLE]
    return ""


def _find_pwd(region: str, line_no: int, used: set[tuple[int, int]]) -> str:
    """在一段文本里找提取码；同一处不会被两个链接重复认领。"""
    for rx in (_PWD_PIPE_RE, _PWD_QUERY_RE, _PWD_LABEL_RE):
        for m in rx.finditer(region):
            key = (line_no, m.start(1))
            if key in used:
                continue
            used.add(key)
            return m.group(1)
    return ""


def _looks_like_link(line: str) -> bool:
    low = line.lower()
    return "http" in low or "www." in low or "链接" in line


def parse_share_text(text: str) -> Parsed:
    """把任意一段文本解析成 [(链接, 提取码)]，外加认不出的行。

    不抛异常：认不出东西是**正常输入**（用户可能只粘了一半），
    该做的是如实告诉他"我认出了什么"，而不是甩一个栈。
    """
    result = Parsed()
    text = str(text or "")[:_MAX_TEXT].replace("\r", "")
    lines = text.split("\n")

    hits: list[_Hit] = []
    for i, raw in enumerate(lines):
        if raw.lstrip().startswith("#"):
            continue
        for m in _URL_RE.finditer(raw):
            token = m.group("token") or _strip_leading_one(m.group("surl") or "")
            if token:
                hits.append(_Hit(i, m.start(), m.end(), token))

    by_line: dict[int, list[_Hit]] = {}
    for h in hits:
        by_line.setdefault(h.line, []).append(h)

    used_pwd: set[tuple[int, int]] = set()
    pwd_lines: set[int] = set()
    consumed: set[int] = set()
    seen: set[str] = set()

    for h in hits:
        consumed.add(h.line)
        if h.token in seen:
            result.duplicates += 1
            continue
        seen.add(h.token)

        line = lines[h.line]
        # 同一行可能有多个链接：本链接的"地盘"到下一个链接为止，
        # 否则第一个链接会把第二个链接的提取码抢走。
        next_start = len(line)
        for other in by_line[h.line]:
            if other.start > h.start:
                next_start = other.start
                break

        pwd = ""
        for region in (line[h.end:next_start], line[:h.start]):
            pwd = _find_pwd(region, h.line, used_pwd)
            if pwd:
                pwd_lines.add(h.line)
                break

        if not pwd:
            # 提取码写在下一行（或下两行）是极常见的格式
            for j in (h.line + 1, h.line + 2):
                if j >= len(lines) or j in by_line or not lines[j].strip():
                    break
                pwd = _find_pwd(lines[j], j, used_pwd)
                if pwd:
                    consumed.add(j)
                    pwd_lines.add(j)
                    break

        if not pwd and h.line > 0:
            j = h.line - 1
            if j not in by_line and lines[j].strip():
                pwd = _find_pwd(lines[j], j, used_pwd)
                if pwd:
                    consumed.add(j)
                    pwd_lines.add(j)

        title = ""
        for j in (h.line - 1, h.line - 2):
            if (j < 0 or j in by_line or j in pwd_lines
                    or not lines[j].strip()):
                break
            title = _title_of(lines[j])
            if title:
                consumed.add(j)
                break

        result.links.append(Link(
            url=f"https://pan.baidu.com/s/1{h.token}",
            pwd=pwd, title=title, line=h.line + 1,
        ))

    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s or i in consumed or s.startswith("#"):
            continue
        if any(b in s for b in _BOILERPLATE):
            continue
        if _looks_like_link(s):
            # 含 http 却没被认出来——大概率是别的网盘的链接，或者链接被截断了。
            # 静默忽略会让用户以为"已经在跑了"，所以要说出来。
            result.unrecognized.append(s[:200])

    return result
