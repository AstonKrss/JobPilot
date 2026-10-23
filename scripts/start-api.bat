@echo off
chcp 65001 >nul
cd /d "%~dp0..\backend"

if not exist "..\.venv\Scripts\python.exe" (
  echo [错误] 未找到虚拟环境，预期：..\.venv\Scripts\python.exe
  echo        请先执行：
  echo          python -m venv .venv
  echo          .venv\Scripts\python.exe -m pip install -r requirements.txt
  exit /b 1
)

echo 启动后端：http://127.0.0.1:8000   文档：http://127.0.0.1:8000/docs
echo 按 Ctrl+C 停止
"..\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
