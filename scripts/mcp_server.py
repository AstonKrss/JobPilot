"""JobPilot MCP server 的 stdio 启动器。

Claude 客户端配置里引用这个文件（绝对路径），不依赖启动时的工作目录：
    claude mcp add jobpilot -- python /…/scripts/mcp_server.py

sys.path 注入 backend 后 import app.*——与 check_*.py 同一套引导模式。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.mcp_server import main  # noqa: E402

if __name__ == "__main__":
    main()
