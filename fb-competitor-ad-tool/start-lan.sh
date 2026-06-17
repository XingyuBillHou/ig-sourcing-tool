#!/bin/bash
# 局域网部署：同一 WiFi/内网下，其他电脑可通过 http://<本机IP>:8501 访问
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
LOG_FILE="$APP_DIR/launch.log"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "========================================"
echo "  FB 广告库浅捞工具 · 局域网模式"
echo "  $(date)"
echo "========================================"

find_python() {
  local candidates=(
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  return 1
}

PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "未检测到 Python 3，请先安装：https://www.python.org/downloads/"
  exit 1
fi

echo "使用 Python: $PYTHON_BIN"
"$PYTHON_BIN" --version

echo ""
echo "正在安装/更新依赖..."
"$PYTHON_BIN" -m pip install --upgrade pip -q
"$PYTHON_BIN" -m pip install -r requirements.txt -q

LAN_IP=""
if command -v ipconfig >/dev/null 2>&1; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
fi
if [ -z "$LAN_IP" ] && command -v hostname >/dev/null 2>&1; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
fi

echo ""
echo "正在启动（监听所有网卡 0.0.0.0:8501）..."
echo "本机访问:   http://localhost:8501"
if [ -n "$LAN_IP" ]; then
  echo "同事访问:   http://${LAN_IP}:8501"
else
  echo "同事访问:   http://<本机局域网IP>:8501"
fi
echo ""
echo "提示：请确保防火墙允许 8501 端口；仅在内网使用，勿暴露到公网。"
echo "如需停止，关闭本终端窗口。"
echo ""

"$PYTHON_BIN" -m streamlit run fb_competitor_ad_app.py \
  --server.address 0.0.0.0 \
  --server.port 8501 \
  --server.headless true
