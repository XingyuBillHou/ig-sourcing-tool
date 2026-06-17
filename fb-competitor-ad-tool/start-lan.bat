@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo ========================================
echo   FB 广告库浅捞工具 - 局域网模式
echo ========================================

where python >nul 2>&1
if errorlevel 1 (
  echo 未检测到 Python，请从 https://www.python.org/downloads/ 安装 Python 3.10+
  pause
  exit /b 1
)

for /f "delims=" %%P in ('where python') do set "PYTHON_BIN=%%P" & goto :found
:found

echo 使用 Python: %PYTHON_BIN%
"%PYTHON_BIN%" -m pip install --upgrade pip -q
"%PYTHON_BIN%" -m pip install -r requirements.txt -q

echo.
echo 正在启动（监听 0.0.0.0:8501）...
echo 本机访问: http://localhost:8501
echo 同事访问: http://^<本机局域网IP^>:8501
echo.
echo 提示：Windows 防火墙若弹出提示，请允许 Python 访问专用网络。
echo.

"%PYTHON_BIN%" -m streamlit run fb_competitor_ad_app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
pause
