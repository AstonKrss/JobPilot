"""一键启动：准备环境 → 起数据库 → 起后端 → 打开浏览器。

为什么把逻辑放在 Python 而不是 bat：bat 处理路径、超时、健康检查都很别扭，
而这里每一步都需要"等它真的就绪"，用 Python 写才看得清。bat 只负责
用正确的解释器调用本文件。

三个设计取舍：
1. postgres.exe 路径不写死。依次查 POSTGRES_BIN 环境变量、.env、
   项目内置 runtime 目录、常见安装位置。换机器只改 .env 一行。
2. 数据库"跟随窗口"——关掉窗口数据库也停。下次双击重新开始，干净可预期。
   想要常驻可以用 stop.bat 之外的进程管理，但那不是本地单用户工具该操心的事。
3. 后端前台运行，日志直接打在窗口里。出错时用户能看到原因，而不是
   "闪一下就没了"——这是这类启动脚本最常见的失败体验。
"""

from __future__ import annotations

import glob
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
if not PYTHON.exists():  # 非 Windows 或用了系统解释器时兜底
    PYTHON = Path(sys.executable)

PG_PORT = 5433
APP_PORT = 8000
APP_URL = f"http://127.0.0.1:{APP_PORT}/"

# 记录"数据库是不是我启动的"。是的话，退出时要负责关掉它 ——
# 否则用户关掉窗口后 postgres 还在后台占着内存，
# 而界面上写着"关闭窗口即可停止服务"，那就成了骗人。
# 如果数据库本来就是用户自己启的，就绝不去动它。
_started_pg: Path | None = None


def say(message: str = "") -> None:
    print(message, flush=True)


def step(title: str) -> None:
    say(f"[*] {title}")


def ok(message: str) -> None:
    say(f"    OK  {message}")


def warn(message: str) -> None:
    say(f"    !!  {message}")


# ---------- 环境检查 ----------


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def read_env_file() -> dict[str, str]:
    """自己解析 .env，不经过 pydantic。

    启动脚本要在依赖装好之前就能跑（首次运行 venv 是空的），
    所以这里不能 import 项目里的任何模块。
    """
    path = ROOT / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def ensure_env_file() -> dict[str, str]:
    """没有 .env 就从模板复制一份，并明确告诉用户还差什么。"""
    env_path = ROOT / ".env"
    example = ROOT / ".env.example"
    if not env_path.exists() and example.exists():
        env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        warn("第一次运行，已从 .env.example 生成 .env")
        warn("请打开 .env 填写 LLM_API_KEY（不填也能启动，但抽取与改写不可用）")
    return read_env_file()


def find_postgres(env: dict[str, str]) -> Path | None:
    """按可靠性从高到低找 postgres.exe。"""
    candidates: list[Path] = []

    for raw in (os.environ.get("POSTGRES_BIN"), env.get("POSTGRES_BIN")):
        if raw:
            path = Path(raw)
            # 允许填目录或可执行文件两种形式
            candidates.append(path / "postgres.exe" if path.is_dir() else path)

    candidates.append(ROOT / "runtime" / "pgsql" / "bin" / "postgres.exe")

    # PATH 里的 postgres（装过 PostgreSQL 并加进 PATH 的情况）
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory and "postgres" in directory.lower():
            candidates.append(Path(directory) / "postgres.exe")

    # conda 环境（本项目开发机就是这种；Windows 上 PostgreSQL 的 binary 包
    # 会被放在 envs/<name>/Library/bin 下）
    for base in ("C:/ProgramData/anaconda3", "C:/Users/%s/anaconda3" % os.environ.get("USERNAME", ""),
                 "D:/Anaconda", "C:/Anaconda3"):
        candidates.extend(Path(p) for p in glob.glob(f"{base}/envs/*/Library/bin/postgres.exe"))

    # 官方安装包（取版本号最大的那个，glob 结果按字典序，需要按版本排）
    for pattern in (
        "C:/Program Files/PostgreSQL/*/bin/postgres.exe",
        "C:/Program Files (x86)/PostgreSQL/*/bin/postgres.exe",
        "D:/Program Files/PostgreSQL/*/bin/postgres.exe",
    ):
        found = sorted(glob.glob(pattern))
        if found:
            candidates.append(Path(found[-1]))

    for path in candidates:
        if path.is_file():
            return path
    return None


# ---------- 数据库 ----------


def start_postgres(env: dict[str, str]) -> bool:
    """确保 5433 有监听。返回是否可用。"""
    global _started_pg
    if port_open(PG_PORT):
        ok(f"数据库已在运行（端口 {PG_PORT}）")
        return True

    data_dir = ROOT / "data" / "pgdata"
    if not data_dir.exists():
        warn("找不到 data/pgdata，说明数据库还没初始化过")
        say("      首次使用需要初始化数据库，步骤见 README 的「准备工作」一节")
        say("      （一键脚本不会替你 initdb：初始化涉及认证方式与建库，")
        say("        自动做错了比报错更难排查）")
        return False

    binary = find_postgres(env)
    if binary is None:
        warn("找不到 postgres.exe")
        say("      请在 .env 里加一行指向它，例如：")
        say("      POSTGRES_BIN=C:/Program Files/PostgreSQL/16/bin/postgres.exe")
        return False

    ok(f"使用 {binary}")
    step(f"启动数据库（端口 {PG_PORT}）")
    creationflags = 0
    if os.name == "nt":
        # 不弹黑窗，也不让它抢当前控制台的 Ctrl+C
        creationflags = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [str(binary), "-D", str(data_dir), "-p", str(PG_PORT)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    # postgres 要读 checkpoint 才能接受连接，冷启动一般 2~5 秒
    started = time.monotonic()
    while time.monotonic() - started < 40:
        if port_open(PG_PORT):
            _started_pg = binary
            ok(f"数据库就绪，用时 {time.monotonic() - started:.1f}s")
            return True
        time.sleep(0.5)

    warn("数据库 40 秒内没起来")
    say(f"      手动排查：{binary} -D {data_dir} -p {PG_PORT}")
    say("      常见原因：上次异常退出留下 postmaster.pid，删掉它再启动")
    return False


# ---------- 后端 ----------


def stop_started_postgres() -> None:
    """关掉本脚本启动的数据库。用 pg_ctl 正常关闭而不是杀进程：
    强杀会让下次启动走 crash recovery，还可能留下 postmaster.pid。"""
    if _started_pg is None:
        return
    pg_ctl = _started_pg.parent / "pg_ctl.exe"
    if pg_ctl.is_file():
        try:
            subprocess.run(
                [str(pg_ctl), "-D", str(ROOT / "data" / "pgdata"), "-m", "fast", "stop"],
                capture_output=True, timeout=30,
            )
            say("[<] 数据库已关闭")
            return
        except (OSError, subprocess.SubprocessError):
            pass
    say("[<] 数据库未关闭，可用 stop.bat 处理")


def wait_and_open_browser() -> None:
    """后台等后端真的能响应，再打开浏览器。

    先开浏览器再起服务是最常见的失败体验：用户看到 ERR_CONNECTION_REFUSED，
    以为坏了。所以这里轮询 /health，等到 200 再开。
    """
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/health", timeout=2) as resp:
                if resp.status == 200:
                    time.sleep(0.3)  # 让首页静态资源也准备好
                    webbrowser.open(APP_URL)
                    say(f"\n[>] 已在浏览器打开 {APP_URL}")
                    say("[>] 关闭这个窗口即可停止服务\n")
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1.0)
    warn(f"后端 90 秒内没就绪，请手动打开 {APP_URL}")


def main() -> int:
    say("=" * 62)
    say("  JobPilot · 本地求职助手")
    say("=" * 62)

    step("检查配置")
    env = ensure_env_file()
    if not env.get("LLM_API_KEY"):
        warn("未配置 LLM_API_KEY：可以浏览与匹配，但 JD/简历抽取与改写会用不了")
    else:
        ok("LLM 密钥已配置")

    step("检查 Python 环境")
    if not PYTHON.exists():
        warn(f"找不到虚拟环境：{PYTHON}")
        say("      请先双击 start.bat（它会自动创建），或在项目根目录执行：")
        say("      python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt")
        return 1
    ok(f"解释器 {PYTHON.name}")

    step("检查数据库")
    if not start_postgres(env):
        say("\n[×] 数据库没起来，后续操作会全部失败，先停在这里。")
        return 1

    if port_open(APP_PORT):
        ok(f"后端已在运行（端口 {APP_PORT}）")
        say(f"\n[>] 直接打开 {APP_URL}\n")
        webbrowser.open(APP_URL)
        return 0

    step("启动后端")
    threading.Thread(target=wait_and_open_browser, daemon=True).start()
    say("    日志会打印在下面。停止服务：Ctrl+C 或直接关窗口\n")

    # 前台运行，让 Ctrl+C 天然传给 uvicorn；cwd 切到 backend 才能 import app
    code = 0
    try:
        code = subprocess.run(
            [
                str(PYTHON), "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(APP_PORT), "--log-level", "info",
            ],
            cwd=str(ROOT / "backend"),
        ).returncode
    except KeyboardInterrupt:
        # Ctrl+C 会同时发给共享控制台的 uvicorn，给它一点时间自己收尾，
        # 否则连接池还没来得及关，日志里会多出一堆无害但吓人的报错
        say("\n正在停止…")
        time.sleep(1.0)
    finally:
        stop_started_postgres()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
