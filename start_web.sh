#!/usr/bin/env bash
# bpan2md 网页界面启动脚本（macOS / Linux）
# 双击或在终端里运行：./start_web.sh
set -e
cd "$(dirname "$0")"
PY=".venv/bin/python"
MIRROR="https://mirrors.aliyun.com/pypi/simple/"

if [ ! -x "$PY" ]; then
  echo "[1/2] 首次运行，正在创建 Python 环境…"
  python3 -m venv .venv || {
    echo "创建失败：请先安装 Python 3.9+（https://www.python.org/downloads/）"
    exit 1
  }
fi

if ! "$PY" -c "import requests" 2>/dev/null; then
  echo "[2/2] 正在下载依赖（可能要一分钟）…"
  "$PY" -m pip install --disable-pip-version-check -r requirements.txt -i "$MIRROR" \
    || "$PY" -m pip install --disable-pip-version-check -r requirements.txt
  "$PY" -c "import requests" 2>/dev/null || {
    echo "依赖安装失败（网络原因？）可以手动执行："
    echo "    $PY -m pip install -r requirements.txt -i $MIRROR"
    exit 1
  }
fi

echo "正在启动网页界面，稍后会自动打开浏览器。关掉这个终端就是退出。"
# Do not reuse stale .pyc files after updating the tool.
exec "$PY" -B run.py web
