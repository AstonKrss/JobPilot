@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set PGBIN=D:\Anaconda\envs\rag-db\Library\bin

if not exist "%PGBIN%\pg_ctl.exe" (
  echo [错误] 未找到 Postgres，预期路径：%PGBIN%
  echo        如需重建：conda create -n rag-db -c conda-forge postgresql pgvector -y
  exit /b 1
)

"%PGBIN%\pg_ctl.exe" -D "data\pgdata" status >nul 2>&1
if %errorlevel%==0 (
  echo Postgres 已在运行（127.0.0.1:5433）
) else (
  echo 正在启动 Postgres...
  "%PGBIN%\pg_ctl.exe" -D "data\pgdata" -o "-p 5433" -l "data\pg.log" -w start
  if %errorlevel% neq 0 (
    echo [错误] 启动失败，请查看 data\pg.log
    exit /b 1
  )
  echo 已启动：127.0.0.1:5433  数据库 ragjob
)
