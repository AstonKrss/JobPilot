"""MCP server 的协议级回归。

不依赖 mcp 客户端库：直接子进程拉起 scripts/mcp_server.py，
对 stdin/stdout 说 MCP 的 JSON-RPC 帧（LSP 风格 Content-Length 分帧，
MCP stdio 用换行分隔 JSON——按官方 spec 逐行 JSON-RPC over stdio）。

三段：
    ① initialize 握手（永远跑，不需要 DB）
    ② tools/list：断言 8 个工具与名称（永远跑）
    ③ tools/call 三个代表工具（需要数据库；库不在则跳过，照 check_radar 惯例）
       radar_overview / job_verdict / skill_gap_priority

用法：
    .venv/Scripts/python.exe scripts/check_mcp.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

PASS = 0
FAIL = 0
EXPECTED_TOOLS = {
    "radar_overview", "job_verdict", "list_jobs", "resume_matches",
    "profile_summary", "skill_gap_expand", "skill_gap_priority", "discover_run",
}


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}{(' — ' + detail) if detail else ''}")


class McpProc:
    """子进程 MCP 客户端的最小实现：发送请求、按行收响应（超时保底）。"""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "mcp_server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", cwd=str(ROOT),
        )
        self.lines: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.next_id = 1

    def _read(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.lines.append(line)

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        rid = self.next_id
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.lines:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == rid and ("result" in msg or "error" in msg):
                    self.lines.remove(line)
                    return msg
            time.sleep(0.1)
        raise TimeoutError(f"{method} 在 {timeout}s 内无响应")

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def main() -> int:
    print("== ① initialize 握手 ==")
    mcp = McpProc()
    try:
        init = mcp.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "check_mcp", "version": "0.1"},
        })
        result = init.get("result") or {}
        info = result.get("serverInfo") or {}
        check("握手成功且 serverInfo.name == jobpilot", info.get("name") == "jobpilot", str(init)[:200])
        check("协议版本回告存在", bool(result.get("protocolVersion")), str(result)[:120])
        mcp.request("notifications/initialized", None, timeout=3)

        print("\n== ② tools/list ==")
        tools_res = mcp.request("tools/list", {})
        tools = (tools_res.get("result") or {}).get("tools") or []
        names = {t["name"] for t in tools}
        check(f"工具数量 == 8（实际 {len(tools)}）", len(tools) == 8, str(names))
        check("工具名与设计一致", names == EXPECTED_TOOLS, str(sorted(names ^ EXPECTED_TOOLS)))
        schemas_ok = all("inputSchema" in t and "description" in t for t in tools)
        check("每个工具都带 inputSchema 与 description", schemas_ok)
        long_desc = next((t for t in tools if t["name"] == "discover_run"), {})
        check("discover_run 描述声明了长任务与预算", "长任务" in (long_desc.get("description") or ""))

        print("\n== ③ tools/call（需要数据库） ==")
        import psycopg

        from app.config import settings

        try:
            probe = psycopg.connect(settings.database_url, connect_timeout=3)
            probe.close()
        except Exception as exc:  # noqa: BLE001
            print(f"  （数据库未启动，跳过数据调用：{exc}）")
            return 0 if FAIL == 0 else 1

        def call_tool(name: str, args: dict, timeout: float = 60.0) -> dict:
            res = mcp.request("tools/call", {"name": name, "arguments": args}, timeout=timeout)
            return res.get("result") or {"_error": res.get("error")}

        # radar_overview
        r1 = call_tool("radar_overview", {"limit": 10})
        content1 = "".join(c.get("text", "") for c in (r1.get("content") or []) if c.get("type") == "text")
        try:
            d1 = json.loads(content1)
        except ValueError:
            d1 = {}
        check("radar_overview 返回结构化概览",
              "total_jobs" in d1 and "verdict" in d1,
              content1[:150] if content1 else str(r1)[:150])
        check("radar_overview verdict 含人话结论",
              bool((d1.get("verdict") or {}).get("text")), str(d1.get("verdict"))[:100])

        # job_verdict：取概览里的第一个岗位
        first_job = ((d1.get("items") or []) or [None])[0]
        if first_job:
            r2 = call_tool("job_verdict", {"job_id": first_job["job_id"]})
            c2 = "".join(c.get("text", "") for c in (r2.get("content") or []) if c.get("type") == "text")
            try:
                d2 = json.loads(c2)
            except ValueError:
                d2 = {}
            check("job_verdict 返回闸门与匹配",
                  "gate" in d2 and "company_score" in d2, c2[:150] if c2 else str(r2)[:150])
        else:
            check("job_verdict（无在库岗位，跳过）", True)

        # skill_gap_priority
        r3 = call_tool("skill_gap_priority", {"limit": 5})
        c3 = "".join(c.get("text", "") for c in (r3.get("content") or []) if c.get("type") == "text")
        try:
            d3 = json.loads(c3)
        except ValueError:
            d3 = {}
        check("skill_gap_priority 返回聚合清单",
              "count" in d3 and "items" in d3, c3[:150] if c3 else str(r3)[:150])

        # 404 语义保留：不存在的岗位
        r4 = call_tool("job_verdict", {"job_id": 99999999})
        c4 = "".join(c.get("text", "") for c in (r4.get("content") or []) if c.get("type") == "text")
        check("不存在的岗位返回 404 人话（不是堆栈）",
              "404" in c4 and "岗位" in c4, c4[:120])
    finally:
        mcp.close()

    total = PASS + FAIL
    print(f"\n{PASS}/{total} 通过")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
