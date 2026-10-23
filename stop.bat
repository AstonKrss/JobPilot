@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [x] 没找到虚拟环境，说明还没启动过。
  pause
  exit /b 1
)

".venv\Scripts\python.exe" scripts\stop.py
echo.
pause
