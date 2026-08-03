#!/usr/bin/env python3
"""cold_store_client.py — lightweight client for a remote MCP memory service

Protocol: MCP streamable-http.
Usage: MemoryCore talks to the cold-tier memory service (remember/recall/
update/forget/stats). The server keeps a fixed session; this client
reconnects on 4xx responses.
"""
import json
import urllib.error
from typing import Any, Dict, List, Optional

from .core.config import MNEMOSYNE_URL  # noqa: E402

TIMEOUT = 10.0


def _parse_sse(body: str) -> Dict[str, Any]:
    """解析 streamable-http 响应 (SSE 格式: event: message / data: {...})。"""
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    # 非 SSE (纯 JSON) 兜底
    return json.loads(body)


class ColdStoreClient:
    """Cold-tier MCP client — provides remember/recall/update/forget/stats.

    Each call initializes a session (small overhead); on 4xx responses it
    clears the session and reconnects. Errors propagate to the caller,
    which decides how to degrade.
    """

    def __init__(self, url: str = MNEMOSYNE_URL, timeout: float = TIMEOUT):
        self.url = url
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self._rpc_id = 0

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        import urllib.request

        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode()
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
        return _parse_sse(body)

    def _initialize(self) -> None:
        resp = self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "memorycore", "version": "0.1.0"},
            },
        })
        # 初始化后通知 server (可选, 保持兼容)
        try:
            self._post({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            })
        except Exception:
            pass
        return resp

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_id:
            self._initialize()
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            resp = self._post(payload)
        except urllib.error.HTTPError as e:
            # 服务重启后旧 session 失效 → 服务器返回 4xx (实测 404)。
            # 清空 session 重新 initialize 再试一次; 仍失败则正常抛错。
            # (Task 3.1 实测: 修复前重启服务后 MemoryCore 持续 404,
            #  直到 MCP server 进程重启 — 此重连逻辑根治该问题)
            if e.code in (400, 401, 404) and self._session_id:
                self._session_id = None
                self._initialize()
                resp = self._post(payload)
            else:
                raise
        if "error" in resp:
            raise RuntimeError(f"cold tier tool {name} error: {resp['error']}")
        content = resp.get("result", {}).get("content", [])
        text = content[0]["text"] if content else "{}"
        try:
            return json.loads(text)
        except Exception:
            try:
                import ast
                return ast.literal_eval(text)
            except Exception:
                return {"raw": text}

    # -- 记忆操作 ------------------------------------------------

    def remember(self, content: str, importance: float = 0.8, scope: str = "global") -> Dict[str, Any]:
        """写入一条冷层记忆。"""
        return self._call_tool("remember", {
            "content": content, "importance": importance, "scope": scope,
        })

    def recall(self, query: str, top_k: int = 5) -> Dict[str, Any]:
        """召回冷层记忆 (混合排序: vector + FTS + importance)。"""
        return self._call_tool("recall", {"query": query, "top_k": top_k})

    def recall_results(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """召回并解析为结构化结果列表 (服务器 content text 是嵌套的单引号 Python dict 字符串)。

        返回 [{id, content, dense_score, keyword_score, fts_score, importance}, ...]
        失败/无结果返回 []。
        """
        raw = self._call_tool("recall", {"query": query, "top_k": top_k})

        # _call_tool 已尝试 json.loads + ast.literal_eval 解析;
        # 若仍返回 {"raw": text} 包装, 此处再试一次 ast.literal_eval
        if "raw" in raw and len(raw) == 1:
            text = raw["raw"]
            data = None
        else:
            # 已解析成功, 直接用
            data = raw
            text = None

        if data is None and text:
            # 先试标准 JSON
            try:
                data = json.loads(text)
            except Exception:
                # 服务器输出 Python 字面量 (单引号 dict), 用 ast.literal_eval
                # 保留 content 内嵌单引号, 不破坏 JSON 结构
                try:
                    import ast
                    data = ast.literal_eval(text)
                except Exception:
                    pass

        if isinstance(data, dict) and data.get("status") == "ok":
            results = data.get("results", [])
            return [dict(r) for r in results]
        return []

    def update(self, memory_id: str, content: str) -> Dict[str, Any]:
        """按 ID 原地更新 (变更必须走 update, 禁止重复 remember)。"""
        return self._call_tool("update", {"memory_id": memory_id, "content": content})

    def forget(self, memory_id: str) -> Dict[str, Any]:
        """按 ID 删除。"""
        return self._call_tool("forget", {"memory_id": memory_id})

    def stats(self) -> Dict[str, Any]:
        """冷层统计: total / embeddings。"""
        return self._call_tool("stats", {})



    def list_all(self) -> List[Dict[str, Any]]:
        """尝试列出冷层全部条目。

        若服务器有 list_all / get_all 工具则调用; 否则抛出 AttributeError。
        返回 [{id, content, importance, ...}, ...]。
        """
        try:
            resp = self._call_tool("list_all", {})
            if isinstance(resp, list):
                return resp
            if isinstance(resp, dict) and resp.get("results"):
                return resp["results"]
            if isinstance(resp, dict) and resp.get("items"):
                return resp["items"]
            # 如果返回了 raw text, 尝试解析
            raw = resp.get("raw", "")
            if raw:
                import json as _json
                try:
                    return _json.loads(raw)
                except Exception:
                    pass
            return []
        except Exception:
            # 尝试 get_all 作为备选工具名
            try:
                resp = self._call_tool("get_all", {})
                if isinstance(resp, list):
                    return resp
                if isinstance(resp, dict):
                    return resp.get("results", resp.get("items", []))
                return []
            except Exception:
                raise AttributeError("list_all unavailable")

if __name__ == "__main__":
    import sys

    c = ColdStoreClient()
    if len(sys.argv) > 1 and sys.argv[1] == "recall":
        q = sys.argv[2] if len(sys.argv) > 2 else "服务器网络"
        print(json.dumps(c.recall(q, top_k=3), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(c.stats(), ensure_ascii=False, indent=2))
