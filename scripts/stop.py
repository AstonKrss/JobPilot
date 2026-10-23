"""停止服务：先让 PostgreSQL 正常关闭，再停后端。

为什么不用 taskkill 直接杀数据库：强杀会让下次启动走 crash recovery，
偶尔还会留下 postmaster.pid 挡住启动（本项目就踩过）。pg_ctl stop -m fast
是让 postgres 自己收尾，几百毫秒的事，值得多这几行代码。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from launch import PG_PORT, APP_PORT, ROOT, find_postgres, port_open, read_env_file  # noqa: E402


def pids_on_port(port: int) -> set[int]:
    """用 netstat 找监听某端口的 PID。不引入 psutil：为一个停止脚本装依赖不值。

    刻意不用 text=True：netstat 输出的编码跟随系统控制台（中文系统是 GBK），
    而 Python 的 locale 推断在 Git Bash 之类的环境里会给出 UTF-8，
    直接解码会抛 UnicodeDecodeError。按字节收再宽松解码最稳。
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, timeout=10
        ).stdout.decode("gbk", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or "LISTEN" not in line.upper():
            continue
        if f":{port}" in parts[1] and parts[-1].isdigit():
            pids.add(int(parts[-1]))
    return pids


def stop_backend() -> None:
    pids = pids_on_port(APP_PORT)
    if not pids:
        print(f"    后端未在运行（端口 {APP_PORT} 无监听）")
        return
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
            else:
                os.kill(pid, signal.SIGTERM)
            print(f"    已停止后端进程 {pid}")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"    停止后端进程 {pid} 失败：{exc}")


def stop_database(env: dict[str, str]) -> None:
    if not port_open(PG_PORT):
        print(f"    数据库未在运行（端口 {PG_PORT} 无监听）")
        return
    binary = find_postgres(env)
    pg_ctl = binary.parent / "pg_ctl.exe" if binary else None
    if pg_ctl and pg_ctl.is_file():
        result = subprocess.run(
            [str(pg_ctl), "-D", str(ROOT / "data" / "pgdata"), "-m", "fast", "stop"],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            print("    数据库已正常关闭")
            return
        detail = (result.stderr or result.stdout).decode("gbk", errors="replace").strip()
        print(f"    pg_ctl 关闭失败：{detail[:120]}")
    for pid in pids_on_port(PG_PORT):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
            print(f"    已强制停止数据库进程 {pid}（下次启动会自动恢复）")
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"    停止数据库进程 {pid} 失败：{exc}")


def main() -> int:
    print("正在停止 JobPilot…")
    stop_backend()
    stop_database(read_env_file())
    print("完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
