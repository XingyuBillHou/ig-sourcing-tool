#!/bin/bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

echo "========================================"
echo "  跨境电商营销工具套件（含投放分析）"
echo "  $(date)"
echo "========================================"

find_python() {
  for candidate in \
    "/opt/homebrew/bin/python3" \
    "/usr/local/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3" \
    "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
  do
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
  read -r -p "按回车键退出..."
  exit 1
fi

echo "Python: $("$PYTHON_BIN" --version)"
echo ""
echo "正在安装依赖（首次运行可能需要 1~2 分钟）..."
"$PYTHON_BIN" -m pip install -q -r requirements-marketing-suite.txt

echo ""
echo "正在启动，浏览器将打开 http://localhost:8501"
echo "关闭本窗口即可停止程序。"
echo ""

"$PYTHON_BIN" -m streamlit run marketing_suite_app.py --server.headless false
