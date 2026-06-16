@echo off
setlocal
cd /d "%~dp0"

echo ========================================
echo   FB 竞品广告拆解工具启动中
echo ========================================

set "PYTHON_BIN="
where python >nul 2>nul && set "PYTHON_BIN=python"
if not defined PYTHON_BIN (
    where py >nul 2>nul && set "PYTHON_BIN=py -3"
)

if not defined PYTHON_BIN (
    echo.
    echo 未检测到 Python 3，请先安装：https://www.python.org/downloads/
    pause
    exit /b 1
)

echo 使用 Python: %PYTHON_BIN%
%PYTHON_BIN% --version
if errorlevel 1 (
    echo Python 命令不可用。
    pause
    exit /b 1
)

echo.
echo 正在安装/更新依赖...
%PYTHON_BIN% -m pip install --upgrade pip
%PYTHON_BIN% -m pip install -r requirements.txt
if errorlevel 1 (
    echo 依赖安装失败，请检查网络连接后重试。
    pause
    exit /b 1
)

echo.
echo 正在启动应用...
echo 浏览器将打开: http://localhost:8501
echo 如需停止程序，请关闭本窗口。
echo.

%PYTHON_BIN% -m streamlit run fb_competitor_ad_app.py --server.headless false
pause
