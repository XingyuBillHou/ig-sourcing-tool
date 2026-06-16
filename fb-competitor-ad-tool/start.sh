#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"
LOG_FILE="$APP_DIR/launch.log"

exec > >(tee -a "$LOG_FILE") 2>&1

echo "========================================"
echo "  FB 竞品广告拆解工具启动中"
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
  echo ""
  echo "未检测到 Python 3，请先安装：https://www.python.org/downloads/"
  read -r -p "按回车键退出..."
  exit 1
fi

echo "使用 Python: $PYTHON_BIN"
"$PYTHON_BIN" --version

echo ""
echo "正在安装/更新依赖..."
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt

echo ""
echo "正在启动应用..."
echo "浏览器将打开: http://localhost:8501"
echo "如需停止程序，请关闭本终端窗口。"
echo ""

"$PYTHON_BIN" -m streamlit run fb_competitor_ad_app.py --server.headless false
