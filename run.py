#!/usr/bin/env python
"""bpan2md 入口。

用法：
    python run.py check
    python run.py inspect "https://pan.baidu.com/s/1xxxx?pwd=ab12"
    python run.py speedtest "https://pan.baidu.com/s/1xxxx?pwd=ab12"
    python run.py run "https://pan.baidu.com/s/1xxxx?pwd=ab12"
    python run.py run --links links.txt
"""

from __future__ import annotations

import sys
from pathlib import Path

# Windows 的控制台默认是 GBK，输出中文提示和符号会直接抛 UnicodeEncodeError。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from bp2md.cli import main  # noqa: E402
except ModuleNotFoundError as exc:  # 缺依赖时给人话，而不是甩一段 traceback
    _name = getattr(exc, "name", "") or str(exc)
    print()
    print(f"启动失败：当前 Python 里缺少 {_name}。")
    print(f"（你正在用的是：{sys.executable}）")
    print()
    print("这个脚本需要 requests。请先建虚拟环境并装依赖：")
    print()
    print("    cd " + str(Path(__file__).resolve().parent))
    print("    python -m venv .venv")
    print(r"    .venv\Scripts\activate        # Windows")
    print("    # source .venv/bin/activate   # macOS / Linux")
    print("    pip install -r requirements.txt")
    print()
    print("装完再重跑原来的命令。注意要用**装好依赖的那个** Python，")
    print("系统里可能同时有好几个 Python，随便一个 python 不一定带 requests。")
    print()
    raise SystemExit(2)

if __name__ == "__main__":
    raise SystemExit(main())
