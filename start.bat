@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

echo ============================================================
echo   首次运行：创建 Python 环境（3-5 分钟，只需这一次）
echo ============================================================
echo.

set "PYEXE="
where py >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE (
  where python >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
  echo [x] 找不到 Python。
  echo     请安装 Python 3.11 以上版本：https://www.python.org/downloads/
  echo     安装时务必勾选 "Add Python to PATH"
  echo.
  pause
  exit /b 1
)

%PYEXE% -m venv .venv
if errorlevel 1 (
  echo [x] 创建虚拟环境失败
  pause
  exit /b 1
)

echo 正在安装依赖，这是最慢的一步，请耐心等待...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [x] 依赖安装失败。若是网络问题，换国内镜像重试：
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  pause
  exit /b 1
)
echo.
echo 环境准备完成。
echo.

:run
".venv\Scripts\python.exe" scripts\launch.py
if errorlevel 1 (
  echo.
  echo [x] 启动失败，原因见上方提示。
  echo.
  pause
)
